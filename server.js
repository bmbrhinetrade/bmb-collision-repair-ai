"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const multer = require("multer");
const PDFDocument = require("pdfkit");
const OpenAI = require("openai");
require("dotenv").config();

const app = express();
const upload = multer({
  storage: multer.memoryStorage(),
  limits: {
    fileSize: 12 * 1024 * 1024,
    files: 20
  }
});

const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || "0.0.0.0";
const publicRoot = __dirname;
const defaultLogoUrl = process.env.BMB_LOGO_URL || "https://framerusercontent.com/images/mYEOmnUYBqG1jUuiKqIAFXijrIM.png?height=926&width=2828";
const defaultLogoLocalPath = path.join(publicRoot, "assets", "bmb-logo.png");

const DEFAULT_SHOP_RATES = {
  bodyLaborPerHour: 83,
  structuralLaborPerHour: 83,
  frameLaborPerHour: 135,
  mechanicalLaborPerHour: 175,
  electricalLaborPerHour: 150,
  paintMaterialsPerPaintHour: 46,
  insideStoragePerDay: 180,
  outsideStoragePerDay: 180,
  towingPerMile: 12
};

const openaiClient = process.env.OPENAI_API_KEY ? new OpenAI({ apiKey: process.env.OPENAI_API_KEY }) : null;

app.use(express.json({ limit: "20mb" }));
app.use(express.urlencoded({ extended: true }));
app.use(express.static(publicRoot));

function nowIso() {
  return new Date().toISOString();
}

function sanitizeVin(vin) {
  return String(vin || "")
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "");
}

function isVinValidShape(vin) {
  return /^[A-HJ-NPR-Z0-9]{17}$/.test(vin);
}

function toSentenceCase(value) {
  if (!value) return "";
  const text = String(value).trim();
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

function safeJsonParse(value, fallback) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function extractJsonObject(text) {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return null;
  const candidate = text.slice(start, end + 1);
  return safeJsonParse(candidate, null);
}

async function decodeVinFromNhtsa(vin) {
  const url = `https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvaluesextended/${vin}?format=json`;
  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(`VIN decode failed (${response.status})`);
  }

  const json = await response.json();
  const result = json && json.Results && json.Results[0] ? json.Results[0] : null;

  if (!result) {
    throw new Error("VIN decode returned no data");
  }

  return {
    vin,
    year: result.ModelYear || "",
    make: result.Make || "",
    model: result.Model || "",
    trim: result.Trim || "",
    bodyClass: result.BodyClass || "",
    vehicleType: result.VehicleType || "",
    engine: [result.EngineModel, result.DisplacementL ? `${result.DisplacementL}L` : "", result.EngineCylinders ? `${result.EngineCylinders} cyl` : ""].filter(Boolean).join(" "),
    fuelType: result.FuelTypePrimary || "",
    driveType: result.DriveType || "",
    transmission: result.TransmissionStyle || "",
    plantCountry: result.PlantCountry || "",
    plantCompany: result.PlantCompanyName || "",
    series: result.Series || "",
    doors: result.Doors || "",
    antiBrakeSystem: result.ABS || "",
    raw: result
  };
}

async function decodeVinFromExtraProvider(vin) {
  const providerUrl = process.env.VIN_EXTRA_PROVIDER_URL;
  if (!providerUrl) {
    return { paintCode: null, paintDescription: null, source: "none" };
  }

  const url = new URL(providerUrl);
  url.searchParams.set("vin", vin);

  const headers = {};
  if (process.env.VIN_EXTRA_PROVIDER_TOKEN) {
    headers.Authorization = `Bearer ${process.env.VIN_EXTRA_PROVIDER_TOKEN}`;
  }

  const response = await fetch(url.toString(), { headers });
  if (!response.ok) {
    return { paintCode: null, paintDescription: null, source: "extra-provider-error" };
  }

  const json = await response.json();

  const paintCode = json.paintCode || json.paint_code || json.colorCode || json.color_code || null;
  const paintDescription = json.paintDescription || json.paint_description || json.colorDescription || json.color_description || null;

  return {
    paintCode,
    paintDescription,
    source: "extra-provider"
  };
}

function buildSystemEntryNotes(system) {
  const all = {
    Mitchell: "Use system-neutral line wording and verify included operations against profile/P-pages logic before final billing.",
    CCC: "Separate not-included operations from refinish assumptions and avoid overlap/duplication.",
    Audatex: "Keep overlap reasoning explicit and tie each add line to a specific job trigger with proof."
  };

  const order = {
    mitchell: ["Mitchell", "CCC", "Audatex"],
    ccc: ["CCC", "Mitchell", "Audatex"],
    audatex: ["Audatex", "Mitchell", "CCC"],
    unknown: ["Mitchell", "CCC", "Audatex"]
  };

  return (order[system] || order.unknown).map((name) => ({
    system: name,
    note: all[name]
  }));
}

function getImpacts(payload) {
  const impacts = Array.isArray(payload.impacts) ? payload.impacts : [];
  return impacts.length ? impacts : ["front"];
}

function normalizeCustomer(input) {
  const source = input && typeof input === "object" ? input : {};
  return {
    fullName: String(source.fullName || "").trim(),
    address: String(source.address || "").trim(),
    phone: String(source.phone || "").trim(),
    email: String(source.email || "").trim()
  };
}

function mergeCustomers(primary, secondary) {
  const a = normalizeCustomer(primary);
  const b = normalizeCustomer(secondary);
  return {
    fullName: b.fullName || a.fullName,
    address: b.address || a.address,
    phone: b.phone || a.phone,
    email: b.email || a.email
  };
}

function asNonNegativeNumber(value, fallback) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return fallback;
  return parsed;
}

function normalizeShopRates(input) {
  const value = input && typeof input === "object" ? input : {};
  return {
    bodyLaborPerHour: asNonNegativeNumber(value.bodyLaborPerHour, DEFAULT_SHOP_RATES.bodyLaborPerHour),
    structuralLaborPerHour: asNonNegativeNumber(value.structuralLaborPerHour, DEFAULT_SHOP_RATES.structuralLaborPerHour),
    frameLaborPerHour: asNonNegativeNumber(value.frameLaborPerHour, DEFAULT_SHOP_RATES.frameLaborPerHour),
    mechanicalLaborPerHour: asNonNegativeNumber(value.mechanicalLaborPerHour, DEFAULT_SHOP_RATES.mechanicalLaborPerHour),
    electricalLaborPerHour: asNonNegativeNumber(value.electricalLaborPerHour, DEFAULT_SHOP_RATES.electricalLaborPerHour),
    paintMaterialsPerPaintHour: asNonNegativeNumber(value.paintMaterialsPerPaintHour, DEFAULT_SHOP_RATES.paintMaterialsPerPaintHour),
    insideStoragePerDay: asNonNegativeNumber(value.insideStoragePerDay, DEFAULT_SHOP_RATES.insideStoragePerDay),
    outsideStoragePerDay: asNonNegativeNumber(value.outsideStoragePerDay, DEFAULT_SHOP_RATES.outsideStoragePerDay),
    towingPerMile: asNonNegativeNumber(value.towingPerMile, DEFAULT_SHOP_RATES.towingPerMile)
  };
}

function normalizeAdditionalCharges(input) {
  const value = input && typeof input === "object" ? input : {};
  return {
    insideStorageDays: asNonNegativeNumber(value.insideStorageDays, 0),
    outsideStorageDays: asNonNegativeNumber(value.outsideStorageDays, 0),
    towingMiles: asNonNegativeNumber(value.towingMiles, 0)
  };
}

function normalizeVehicleLabel(input) {
  const value = input && typeof input === "object" ? input : {};
  return {
    vin: sanitizeVin(value.vin || ""),
    paintCode: String(value.paintCode || "").trim(),
    paintDescription: String(value.paintDescription || "").trim(),
    modelCode: String(value.modelCode || "").trim(),
    productionDate: String(value.productionDate || "").trim(),
    confidence: String(value.confidence || "").toLowerCase() || "low",
    source: String(value.source || "").trim() || "none",
    notes: Array.isArray(value.notes) ? value.notes.map((note) => String(note || "").trim()).filter(Boolean) : []
  };
}

function mergeVehicleLabels(primary, secondary) {
  const first = normalizeVehicleLabel(primary);
  const second = normalizeVehicleLabel(secondary);

  return {
    vin: second.vin || first.vin,
    paintCode: second.paintCode || first.paintCode,
    paintDescription: second.paintDescription || first.paintDescription,
    modelCode: second.modelCode || first.modelCode,
    productionDate: second.productionDate || first.productionDate,
    confidence: second.confidence || first.confidence || "low",
    source: second.source !== "none" ? second.source : first.source,
    notes: [...new Set([...(first.notes || []), ...(second.notes || [])])]
  };
}

function applyVehicleLabelToVehicle(vehicle, labelInput) {
  const base = vehicle && typeof vehicle === "object" ? { ...vehicle } : {};
  const label = normalizeVehicleLabel(labelInput);

  if (label.vin) base.vin = label.vin;
  if (label.paintCode) base.paintCode = label.paintCode;
  if (label.paintDescription) base.paintDescription = label.paintDescription;

  return base;
}

function appendAssumptions(report, entries) {
  if (!report || typeof report !== "object") return;
  const list = Array.isArray(entries) ? entries : [];
  report.assumptions = Array.isArray(report.assumptions) ? report.assumptions : [];
  for (const raw of list) {
    const text = String(raw || "").trim();
    if (text && !report.assumptions.includes(text)) {
      report.assumptions.push(text);
    }
  }
}

function classifyLaborType(component, action) {
  const text = `${component || ""} ${action || ""}`.toLowerCase();

  if (/(frame|straighten|rail|pull|bench|measure)/.test(text)) return "frame";
  if (/(support|rocker|pillar|quarter panel|rear body panel|apron|tie bar|structural|srs)/.test(text)) return "structural";
  if (/(cooling stack|radiator|condenser|suspension|steering|alignment|wheel|engine|mechanical|ac )/.test(text)) return "mechanical";
  if (/(sensor|camera|radar|module|harness|electrical|headlamp|tail lamp|lamp|scan|calibration|aim)/.test(text)) return "electrical";
  return "body";
}

function estimateLaborHours(component, action, severity) {
  const text = `${component || ""}`.toLowerCase();
  const act = String(action || "").toLowerCase();
  let hours = 1.5;

  if (act.includes("replace")) hours = 2.5;
  else if (act.includes("repair")) hours = 3.0;
  else if (act.includes("r&i") || act.includes("r&r")) hours = 1.2;
  else if (act.includes("inspect")) hours = 0.5;
  else if (act.includes("overhaul")) hours = 2.0;

  if (/(bumper cover|grille|emblem)/.test(text)) hours += 0.7;
  if (/(headlamp|tail lamp|lamp)/.test(text)) hours += 0.4;
  if (/(liner|shield|undertray|splash)/.test(text)) hours -= 0.3;
  if (/(reinforcement|absorber)/.test(text)) hours += 0.5;
  if (/(support|cooling stack|radiator|condenser)/.test(text)) hours += 1.0;
  if (/(door shell)/.test(text)) hours += 1.5;
  if (/(quarter panel|rocker|pillar)/.test(text)) hours += 2.0;
  if (/(srs|restraint)/.test(text)) hours += 1.0;

  if (severity === "structural") hours *= 1.2;
  if (severity === "srs") hours *= 1.25;

  return Math.max(0.3, Number(hours.toFixed(2)));
}

function estimatePaintHours(component, action) {
  const text = `${component || ""}`.toLowerCase();
  const act = String(action || "").toLowerCase();
  if (!(act.includes("replace") || act.includes("repair"))) return 0;

  let paintHours = 0;
  if (/(bumper|fender|door|quarter|hood|decklid|trunk|panel)/.test(text)) {
    paintHours = act.includes("repair") ? 2.2 : 1.8;
  }
  if (/(bumper cover)/.test(text)) {
    paintHours += 0.6;
  }
  return Number(Math.max(0, paintHours).toFixed(2));
}

function getLaborRateByType(laborType, rates) {
  if (laborType === "body" || laborType === "paint") return rates.bodyLaborPerHour;
  if (laborType === "structural") return rates.structuralLaborPerHour;
  if (laborType === "frame") return rates.frameLaborPerHour;
  if (laborType === "mechanical") return rates.mechanicalLaborPerHour;
  if (laborType === "electrical") return rates.electricalLaborPerHour;
  return rates.bodyLaborPerHour;
}

function applyEstimateCalculations(report, ratesInput, chargesInput) {
  const rates = normalizeShopRates(ratesInput);
  const charges = normalizeAdditionalCharges(chargesInput);
  const parts = normalizeParts(report && report.parts);

  const severity = report && report.summary ? report.summary.severity : "functional";
  const baseLines = Array.isArray(report.output1) ? report.output1 : [];

  const lineItems = baseLines.map((row) => {
    const laborType = classifyLaborType(row.component, row.action);
    const laborHours = estimateLaborHours(row.component, row.action, severity);
    const ratePerHour = getLaborRateByType(laborType, rates);
    const laborTotal = Number((laborHours * ratePerHour).toFixed(2));
    const paintHours = estimatePaintHours(row.component, row.action);
    return {
      component: row.component || "",
      action: row.action || "",
      laborType,
      laborHours,
      ratePerHour,
      laborTotal,
      paintHours,
      notes: row.notes || ""
    };
  });

  const laborByType = {
    body: { hours: 0, rate: rates.bodyLaborPerHour, total: 0 },
    structural: { hours: 0, rate: rates.structuralLaborPerHour, total: 0 },
    frame: { hours: 0, rate: rates.frameLaborPerHour, total: 0 },
    mechanical: { hours: 0, rate: rates.mechanicalLaborPerHour, total: 0 },
    electrical: { hours: 0, rate: rates.electricalLaborPerHour, total: 0 },
    paint: { hours: 0, rate: rates.bodyLaborPerHour, total: 0 }
  };

  for (const item of lineItems) {
    const bucket = laborByType[item.laborType] || laborByType.body;
    bucket.hours = Number((bucket.hours + item.laborHours).toFixed(2));
    bucket.total = Number((bucket.total + item.laborTotal).toFixed(2));
    laborByType.paint.hours = Number((laborByType.paint.hours + item.paintHours).toFixed(2));
  }

  laborByType.paint.total = Number((laborByType.paint.hours * laborByType.paint.rate).toFixed(2));

  const laborSubtotal = Number(
    Object.values(laborByType).reduce((sum, item) => sum + Number(item.total || 0), 0).toFixed(2)
  );
  const paintMaterialsTotal = Number((laborByType.paint.hours * rates.paintMaterialsPerPaintHour).toFixed(2));
  const insideStorageTotal = Number((charges.insideStorageDays * rates.insideStoragePerDay).toFixed(2));
  const outsideStorageTotal = Number((charges.outsideStorageDays * rates.outsideStoragePerDay).toFixed(2));
  const towingTotal = Number((charges.towingMiles * rates.towingPerMile).toFixed(2));
  const partsSubtotal = Number(asNonNegativeNumber(parts.subtotal, 0).toFixed(2));
  const grandTotal = Number(
    (laborSubtotal + paintMaterialsTotal + partsSubtotal + insideStorageTotal + outsideStorageTotal + towingTotal).toFixed(2)
  );

  report.rates = rates;
  report.charges = charges;
  report.parts = {
    ...parts,
    subtotal: partsSubtotal
  };
  report.calculation = {
    lineItems,
    laborByType,
    rates: {
      insideStoragePerDay: rates.insideStoragePerDay,
      outsideStoragePerDay: rates.outsideStoragePerDay,
      towingPerMile: rates.towingPerMile
    },
    charges,
    laborSubtotal,
    paintMaterialsTotal,
    partsSubtotal,
    insideStorageTotal,
    outsideStorageTotal,
    towingTotal,
    grandTotal
  };
  return report;
}

function buildLicensePrompt() {
  return `Extract customer contact data from this driver's license image.
Return strict JSON only with this shape:
{
  "fullName": string,
  "address": string,
  "phone": string,
  "email": string,
  "confidence": "high"|"medium"|"low",
  "notes": string[]
}
Rules:
- If a field is not visible, return empty string for that field.
- Do not hallucinate. Do not infer unavailable phone/email.
- Use one-line address string if visible.`;
}

async function extractCustomerFromLicense(licenseFile) {
  if (!openaiClient || !licenseFile) {
    return {
      customer: normalizeCustomer({}),
      confidence: "low",
      notes: ["License extraction unavailable (missing file or OpenAI key)."],
      source: "license-fallback"
    };
  }

  const model = process.env.OPENAI_VISION_MODEL || "gpt-4.1";
  const base64 = licenseFile.buffer.toString("base64");
  const mime = licenseFile.mimetype || "image/jpeg";

  const response = await openaiClient.responses.create({
    model,
    input: [
      {
        role: "user",
        content: [
          { type: "input_text", text: buildLicensePrompt() },
          { type: "input_image", image_url: `data:${mime};base64,${base64}` }
        ]
      }
    ],
    max_output_tokens: 700
  });

  const text = response.output_text || "";
  const parsed = safeJsonParse(text, null) || extractJsonObject(text);
  if (!parsed) {
    throw new Error("License extraction response was not valid JSON");
  }

  return {
    customer: normalizeCustomer(parsed),
    confidence: parsed.confidence || "low",
    notes: Array.isArray(parsed.notes) ? parsed.notes : [],
    source: "license-ai"
  };
}

function buildVehicleLabelPrompt() {
  return `Extract structured data from this vehicle door-jamb label image (barcode/sticker).
Return strict JSON only:
{
  "vin": string,
  "paintCode": string,
  "paintDescription": string,
  "modelCode": string,
  "productionDate": string,
  "confidence": "high"|"medium"|"low",
  "notes": string[]
}
Rules:
- Return empty string when a field is not readable.
- Do not hallucinate VIN or paint code.
- VIN must be exactly what is visible on label if readable.
- If barcode cannot be decoded, use visible printed text only.`;
}

async function extractVehicleLabelFromImage(vehicleLabelFile) {
  if (!openaiClient || !vehicleLabelFile) {
    return {
      vehicleLabel: normalizeVehicleLabel({}),
      source: "vehicle-label-fallback",
      confidence: "low",
      notes: ["Door-jamb extraction unavailable (missing file or OpenAI key)."]
    };
  }

  const model = process.env.OPENAI_VISION_MODEL || "gpt-4.1";
  const base64 = vehicleLabelFile.buffer.toString("base64");
  const mime = vehicleLabelFile.mimetype || "image/jpeg";

  const response = await openaiClient.responses.create({
    model,
    input: [
      {
        role: "user",
        content: [
          { type: "input_text", text: buildVehicleLabelPrompt() },
          { type: "input_image", image_url: `data:${mime};base64,${base64}` }
        ]
      }
    ],
    max_output_tokens: 700
  });

  const text = response.output_text || "";
  const parsed = safeJsonParse(text, null) || extractJsonObject(text);
  if (!parsed) {
    throw new Error("Door-jamb extraction response was not valid JSON");
  }

  const normalized = normalizeVehicleLabel({
    vin: parsed.vin,
    paintCode: parsed.paintCode,
    paintDescription: parsed.paintDescription,
    modelCode: parsed.modelCode,
    productionDate: parsed.productionDate,
    confidence: parsed.confidence || "low",
    notes: Array.isArray(parsed.notes) ? parsed.notes : [],
    source: "vehicle-label-ai"
  });

  return {
    vehicleLabel: normalized,
    source: "vehicle-label-ai",
    confidence: normalized.confidence,
    notes: normalized.notes
  };
}

function normalizeParts(parts) {
  const value = parts && typeof parts === "object" ? parts : {};
  const itemsRaw = Array.isArray(value.items) ? value.items : [];
  const items = itemsRaw.map((entry) => {
    const quantity = asNonNegativeNumber(entry.quantity, 1);
    const listPrice = asNonNegativeNumber(entry.listPrice, 0);
    const computedLineTotal = Number((quantity * listPrice).toFixed(2));
    const lineTotal = asNonNegativeNumber(entry.lineTotal, computedLineTotal);
    return {
      component: String(entry.component || "").trim(),
      partNumber: String(entry.partNumber || "").trim(),
      description: String(entry.description || "").trim(),
      quantity,
      listPrice,
      lineTotal,
      source: String(entry.source || "").trim() || "unknown"
    };
  });

  const computedSubtotal = Number(items.reduce((sum, item) => sum + Number(item.lineTotal || 0), 0).toFixed(2));
  const subtotal = asNonNegativeNumber(value.subtotal, computedSubtotal);

  return {
    source: String(value.source || "").trim() || "not-configured",
    items,
    subtotal,
    assumptions: Array.isArray(value.assumptions) ? value.assumptions.map((note) => String(note || "").trim()).filter(Boolean) : []
  };
}

function deriveBrokenComponents(report) {
  const rows = Array.isArray(report && report.output1) ? report.output1 : [];
  const components = [];
  for (const row of rows) {
    const component = String(row.component || "").trim();
    const action = String(row.action || "").toLowerCase();
    if (!component) continue;
    if (/(replace|repair|r&i|r&r|section|blend)/.test(action)) {
      components.push(component);
    }
  }
  return [...new Set(components)];
}

function buildPlaceholderOemParts(components, reason) {
  const items = components.map((component) => ({
    component,
    partNumber: "OEM-LOOKUP-REQUIRED",
    description: "OEM part lookup required",
    quantity: 1,
    listPrice: 0,
    lineTotal: 0,
    source: "placeholder"
  }));

  return normalizeParts({
    source: "placeholder",
    items,
    assumptions: [reason]
  });
}

async function fetchOemPartsFromProvider(vehicle, components) {
  if (!components.length) {
    return normalizeParts({
      source: "none",
      items: [],
      assumptions: ["No broken components were identified for OEM parts lookup."]
    });
  }

  const providerUrl = process.env.OEM_PARTS_PROVIDER_URL;
  if (!providerUrl) {
    return buildPlaceholderOemParts(
      components,
      "OEM parts provider is not configured. Add OEM_PARTS_PROVIDER_URL and token for live dealership part numbers/pricing."
    );
  }

  const headers = { "Content-Type": "application/json" };
  if (process.env.OEM_PARTS_PROVIDER_TOKEN) {
    headers.Authorization = `Bearer ${process.env.OEM_PARTS_PROVIDER_TOKEN}`;
  }

  try {
    const response = await fetch(providerUrl, {
      method: "POST",
      headers,
      body: JSON.stringify({
        vehicle: {
          vin: vehicle.vin || "",
          year: vehicle.year || "",
          make: vehicle.make || "",
          model: vehicle.model || "",
          trim: vehicle.trim || ""
        },
        components
      })
    });

    if (!response.ok) {
      return buildPlaceholderOemParts(
        components,
        `OEM parts provider request failed (${response.status}). Using placeholder parts without pricing.`
      );
    }

    const json = await response.json();
    const rawItems = Array.isArray(json.items) ? json.items : (Array.isArray(json.parts) ? json.parts : []);
    if (!rawItems.length) {
      return buildPlaceholderOemParts(
        components,
        "OEM parts provider returned no parts for current components."
      );
    }

    const items = rawItems.map((entry) => ({
      component: entry.component || entry.partName || "",
      partNumber: entry.partNumber || entry.oemPartNumber || entry.number || "",
      description: entry.description || entry.partDescription || "",
      quantity: asNonNegativeNumber(entry.quantity, 1),
      listPrice: asNonNegativeNumber(entry.listPrice, asNonNegativeNumber(entry.price, 0)),
      lineTotal: asNonNegativeNumber(
        entry.lineTotal,
        Number((asNonNegativeNumber(entry.quantity, 1) * asNonNegativeNumber(entry.listPrice, asNonNegativeNumber(entry.price, 0))).toFixed(2))
      ),
      source: entry.source || entry.vendor || "oem-provider"
    }));

    return normalizeParts({
      source: "oem-provider",
      items,
      subtotal: asNonNegativeNumber(json.subtotal, NaN),
      assumptions: Array.isArray(json.assumptions) ? json.assumptions : []
    });
  } catch (error) {
    return buildPlaceholderOemParts(
      components,
      `OEM parts provider request failed (${error.message || "network error"}). Using placeholder parts without pricing.`
    );
  }
}

async function buildOemPartsForReport(report) {
  const vehicle = report && report.vehicle ? report.vehicle : {};
  const components = deriveBrokenComponents(report);
  return fetchOemPartsFromProvider(vehicle, components);
}

function buildRuleBasedFallback(payload, decodedVehicle, photoCount, customer, vehicleLabel) {
  const impacts = getImpacts(payload);
  const severity = payload.severity || "functional";

  const output1 = [];
  const primaryFront = [
    ["Front bumper cover", severity === "cosmetic" ? "Repair" : "Replace", "RESTORE"],
    ["Grille / emblem assembly", severity === "cosmetic" ? "Inspect - Pending teardown" : "R&I", "ACCESS"],
    ["LF headlamp assembly", severity === "cosmetic" ? "Inspect - Pending teardown" : "Replace", "VERIFY"],
    ["RF headlamp assembly", severity === "cosmetic" ? "Inspect - Pending teardown" : "Replace", "VERIFY"],
    ["Front undertray / splash shield", "R&I", "ACCESS"],
    ["Front bumper reinforcement", "Inspect - Pending teardown", "VERIFY"],
    ["Cooling stack (radiator/condenser/shutters)", "Inspect - Pending teardown", "VERIFY"]
  ];

  const primaryRear = [
    ["Rear bumper cover", severity === "cosmetic" ? "Repair" : "Replace", "RESTORE"],
    ["Rear bumper reinforcement", "Inspect - Pending teardown", "VERIFY"],
    ["LH tail lamp assembly", severity === "cosmetic" ? "Inspect - Pending teardown" : "Replace", "VERIFY"],
    ["RH tail lamp assembly", severity === "cosmetic" ? "Inspect - Pending teardown" : "Replace", "VERIFY"]
  ];

  const side = [
    ["LF/LR or RF/RR door shell(s)", severity === "cosmetic" ? "Repair" : "Inspect - Pending teardown", "RESTORE"],
    ["Quarter panel / rocker", "Inspect - Pending teardown", "VERIFY"],
    ["Wheelhouse liner", "R&I", "ACCESS"]
  ];

  if (impacts.includes("front")) {
    for (const row of primaryFront) {
      output1.push({
        component: row[0],
        action: row[1],
        laborBucket: row[2],
        notes: "Rule-based preliminary from selected impact + severity.",
        confidence: photoCount >= 10 ? "Med" : "Low"
      });
    }
  }

  if (impacts.includes("rear")) {
    for (const row of primaryRear) {
      output1.push({
        component: row[0],
        action: row[1],
        laborBucket: row[2],
        notes: "Rule-based preliminary from selected impact + severity.",
        confidence: photoCount >= 10 ? "Med" : "Low"
      });
    }
  }

  if (impacts.includes("left") || impacts.includes("right")) {
    for (const row of side) {
      output1.push({
        component: row[0],
        action: row[1],
        laborBucket: row[2],
        notes: "Rule-based preliminary from selected impact + severity.",
        confidence: "Low"
      });
    }
  }

  const output2 = [
    {
      missingOperation: "Pre-repair diagnostic scan",
      category: "Verify",
      appliesTo: "Vehicle-level",
      why: "Baseline DTC capture before repair path.",
      bestProof: "Pre-scan report with VIN/date/time",
      billNowOrPending: "Bill now"
    },
    {
      missingOperation: "Post-repair diagnostic scan",
      category: "Verify",
      appliesTo: "Vehicle-level",
      why: "Confirm post-repair DTC status.",
      bestProof: "Post-scan report with VIN/date/time",
      billNowOrPending: "Bill now"
    },
    {
      missingOperation: "Clips/retainers/one-time fasteners as required",
      category: "Restore",
      appliesTo: "Disturbed assemblies",
      why: "One-time-use hardware frequently replaced during R&I.",
      bestProof: "Teardown photos + parts invoice",
      billNowOrPending: "Pending teardown"
    },
    {
      missingOperation: "Calibration / aiming as required",
      category: "Verify",
      appliesTo: "ADAS/lamp disturbed components",
      why: "Sensor or lamp disturbance may require final calibration/aim.",
      bestProof: "Calibration/aim printout",
      billNowOrPending: "Pending teardown"
    }
  ];

  const output3 = [
    "Preliminary estimate is based on available photos and visible damage only. Final line commitment remains subject to teardown findings and OEM procedure verification.",
    "Access operations are separated from base repair lines to make the repair physically possible and avoid duplicate billing.",
    "Restore operations are included for disturbed one-time-use materials and corrosion/NVH recovery where teardown confirms disturbance.",
    "Verify operations (scan/calibration/aim/alignment) are trigger-backed and should be supported with scan and calibration documentation."
  ];

  const output4 = [
    "Confirm hidden bracket/tab damage behind impact covers.",
    "Confirm sensor mounts and harness retention points before final calibration commitment.",
    "Confirm cooling stack, support structures, and dimensional condition on front-path damage.",
    "Collect additional teardown photos for low-confidence areas."
  ];

  return {
    source: "rules-fallback",
    generatedAt: nowIso(),
    customer: normalizeCustomer(customer || payload.customer),
    vehicleLabel: normalizeVehicleLabel(vehicleLabel),
    vehicle: decodedVehicle,
    summary: {
      estimateType: payload.estimateType || "preliminary",
      impacts,
      severity,
      photoCount,
      confidence: photoCount >= 10 ? "medium" : "low"
    },
    output1,
    output2,
    output3,
    output4,
    output5: buildSystemEntryNotes(payload.system || "unknown"),
    assumptions: [
      "This is a fallback estimate generated without AI vision model output.",
      "Paint code is not guaranteed from VIN without an external provider or OEM label reference."
    ]
  };
}

function buildVisionPrompt(payload, decodedVehicle) {
  const impacts = getImpacts(payload);

  return `You are a collision repair estimator assistant.
Return JSON only with this exact schema keys:
{
  "source": string,
  "generatedAt": string,
  "customer": {
    "fullName": string,
    "address": string,
    "phone": string,
    "email": string
  },
  "vehicle": {
    "vin": string,
    "year": string,
    "make": string,
    "model": string,
    "trim": string,
    "paintCode": string,
    "paintDescription": string,
    "paintConfidence": "High"|"Med"|"Low"
  },
  "summary": {
    "estimateType": string,
    "impacts": string[],
    "severity": string,
    "photoCount": number,
    "confidence": "high"|"medium"|"low"
  },
  "output1": [
    {
      "component": string,
      "action": string,
      "laborBucket": "ACCESS"|"RESTORE"|"VERIFY",
      "notes": string,
      "confidence": "High"|"Med"|"Low"
    }
  ],
  "output2": [
    {
      "missingOperation": string,
      "category": "Access"|"Restore"|"Refinish"|"Verify",
      "appliesTo": string,
      "why": string,
      "bestProof": string,
      "billNowOrPending": "Bill now"|"Pending teardown"
    }
  ],
  "output3": string[],
  "output4": string[],
  "output5": [
    {
      "system": "Mitchell"|"CCC"|"Audatex",
      "note": string
    }
  ],
  "assumptions": string[]
}

Rules:
- Use Access / Restore / Verify logic.
- Produce all 5 deliverables.
- Keep lines carrier-proof and concise.
- Never invent OEM MSRP.
- If paint code is not visible from reliable source, set paintCode to "Unknown" and include assumption.
- Preserve provided customer details. Only add/update customer fields from license text if clearly visible.
- Treat hidden items as pending teardown.

Context:
- System: ${payload.system || "unknown"}
- Estimate type: ${payload.estimateType || "preliminary"}
- Impacts: ${impacts.join(", ")}
- Severity: ${payload.severity || "functional"}
- Drivable: ${payload.drivable || "unknown"}
- User notes: ${payload.notes || "none"}
- Customer baseline: ${JSON.stringify(normalizeCustomer(payload.customer))}
- Door-jamb label baseline: ${JSON.stringify(normalizeVehicleLabel(payload.vehicleLabel))}
- VIN decoded baseline: ${JSON.stringify({
    vin: decodedVehicle.vin || "",
    year: decodedVehicle.year || "",
    make: decodedVehicle.make || "",
    model: decodedVehicle.model || "",
    trim: decodedVehicle.trim || "",
    bodyClass: decodedVehicle.bodyClass || "",
    paintCode: decodedVehicle.paintCode || "",
    paintDescription: decodedVehicle.paintDescription || ""
  })}`;
}

async function generateVisionEstimate(payload, decodedVehicle, photos) {
  const model = process.env.OPENAI_VISION_MODEL || "gpt-4.1";

  const content = [
    {
      type: "input_text",
      text: buildVisionPrompt(payload, decodedVehicle)
    }
  ];

  const cappedPhotos = photos.slice(0, 12);
  for (const file of cappedPhotos) {
    const base64 = file.buffer.toString("base64");
    const mime = file.mimetype || "image/jpeg";
    content.push({
      type: "input_image",
      image_url: `data:${mime};base64,${base64}`
    });
  }

  const response = await openaiClient.responses.create({
    model,
    input: [
      {
        role: "user",
        content
      }
    ],
    max_output_tokens: 3500
  });

  const text = response.output_text || "";
  const parsed = safeJsonParse(text, null) || extractJsonObject(text);

  if (!parsed) {
    throw new Error("AI response was not valid JSON");
  }

  return parsed;
}

function normalizeReport(report, payload, decodedVehicle, photoCount, customer) {
  const safe = report || {};
  const mergedCustomer = mergeCustomers(payload.customer, mergeCustomers(customer, safe.customer));
  const mergedVehicleLabel = mergeVehicleLabels(payload.vehicleLabel, safe.vehicleLabel);

  const vehicle = safe.vehicle || {};
  const finalVehicle = {
    vin: vehicle.vin || mergedVehicleLabel.vin || decodedVehicle.vin || payload.vin || "",
    year: vehicle.year || decodedVehicle.year || String(payload.year || ""),
    make: vehicle.make || decodedVehicle.make || payload.make || "",
    model: vehicle.model || decodedVehicle.model || payload.model || "",
    trim: vehicle.trim || decodedVehicle.trim || "",
    paintCode: vehicle.paintCode || mergedVehicleLabel.paintCode || decodedVehicle.paintCode || "Unknown",
    paintDescription: vehicle.paintDescription || mergedVehicleLabel.paintDescription || decodedVehicle.paintDescription || "Unknown",
    paintConfidence: vehicle.paintConfidence || (mergedVehicleLabel.paintCode || decodedVehicle.paintCode ? "High" : "Low")
  };

  const summary = safe.summary || {};

  const output1 = Array.isArray(safe.output1) ? safe.output1 : [];
  const output2 = Array.isArray(safe.output2) ? safe.output2 : [];
  const output3 = Array.isArray(safe.output3) ? safe.output3 : [];
  const output4 = Array.isArray(safe.output4) ? safe.output4 : [];

  const output5 = Array.isArray(safe.output5) && safe.output5.length
    ? safe.output5
    : buildSystemEntryNotes(payload.system || "unknown");

  const assumptions = Array.isArray(safe.assumptions) ? safe.assumptions : [];

  return {
    source: safe.source || "ai-vision",
    generatedAt: safe.generatedAt || nowIso(),
    customer: mergedCustomer,
    vehicleLabel: mergedVehicleLabel,
    vehicle: finalVehicle,
    summary: {
      estimateType: summary.estimateType || payload.estimateType || "preliminary",
      impacts: Array.isArray(summary.impacts) && summary.impacts.length ? summary.impacts : getImpacts(payload),
      severity: summary.severity || payload.severity || "functional",
      photoCount: Number(summary.photoCount || photoCount || 0),
      confidence: summary.confidence || (photoCount >= 10 ? "medium" : "low")
    },
    output1,
    output2,
    output3,
    output4,
    output5,
    assumptions
  };
}

function writeHeading(doc, text, size = 13) {
  doc.moveDown(0.4);
  doc.font("Helvetica-Bold").fontSize(size).text(text);
  doc.moveDown(0.25);
}

function writeLine(doc, text, options = {}) {
  doc.font("Helvetica").fontSize(options.size || 10).text(text, options);
}

function writeBullets(doc, items) {
  if (!items.length) {
    writeLine(doc, "- None");
    return;
  }

  for (const item of items) {
    writeLine(doc, `- ${item}`);
  }
}

async function resolveLogoBuffer() {
  const configuredLocalPath = process.env.BMB_LOGO_FILE ? path.resolve(publicRoot, process.env.BMB_LOGO_FILE) : "";
  const localCandidates = [configuredLocalPath, defaultLogoLocalPath].filter(Boolean);

  for (const filePath of localCandidates) {
    try {
      if (fs.existsSync(filePath)) {
        return fs.readFileSync(filePath);
      }
    } catch {
      // Continue to next candidate.
    }
  }

  if (!defaultLogoUrl) {
    return null;
  }

  try {
    const response = await fetch(defaultLogoUrl, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) {
      return null;
    }

    const contentType = String(response.headers.get("content-type") || "");
    if (!contentType.includes("image")) {
      return null;
    }

    const arrayBuffer = await response.arrayBuffer();
    return Buffer.from(arrayBuffer);
  } catch {
    return null;
  }
}

function buildPdfBuffer(report, logoBuffer) {
  return new Promise((resolve, reject) => {
    const doc = new PDFDocument({ size: "LETTER", margin: 42 });
    const chunks = [];

    doc.on("data", (chunk) => chunks.push(chunk));
    doc.on("end", () => resolve(Buffer.concat(chunks)));
    doc.on("error", reject);

    if (logoBuffer) {
      try {
        doc.image(logoBuffer, 42, 32, { fit: [130, 56], align: "left", valign: "center" });
      } catch {
        // Continue without logo if image decode fails.
      }
    }

    doc.font("Helvetica-Bold").fontSize(18).text("BMB Collision Repair AI Estimate", logoBuffer ? 185 : 42, 42);
    doc.moveDown(0.2);
    doc.font("Helvetica").fontSize(10).text(`Generated: ${report.generatedAt || nowIso()}`, logoBuffer ? 185 : 42);

    writeHeading(doc, "Vehicle");
    const v = report.vehicle || {};
    const vl = normalizeVehicleLabel(report.vehicleLabel);
    writeLine(doc, `VIN: ${v.vin || "N/A"}`);
    writeLine(doc, `Year/Make/Model: ${[v.year, v.make, v.model].filter(Boolean).join(" ") || "N/A"}`);
    writeLine(doc, `Trim: ${v.trim || "N/A"}`);
    writeLine(doc, `Paint: ${v.paintCode || "Unknown"} ${v.paintDescription ? `(${v.paintDescription})` : ""}`);
    if (vl.vin || vl.paintCode || vl.modelCode || vl.productionDate) {
      writeHeading(doc, "Door-Jamb Label");
      writeLine(doc, `Label VIN: ${vl.vin || "N/A"}`);
      writeLine(doc, `Label paint: ${vl.paintCode || "Unknown"} ${vl.paintDescription ? `(${vl.paintDescription})` : ""}`);
      writeLine(doc, `Model code: ${vl.modelCode || "N/A"}`);
      writeLine(doc, `Production date: ${vl.productionDate || "N/A"}`);
    }

    writeHeading(doc, "Customer");
    const c = report.customer || {};
    writeLine(doc, `Full name: ${c.fullName || "N/A"}`);
    writeLine(doc, `Address: ${c.address || "N/A"}`);
    writeLine(doc, `Phone: ${c.phone || "N/A"}`);
    writeLine(doc, `Email: ${c.email || "N/A"}`);

    const s = report.summary || {};
    const calc = report.calculation || {};
    writeHeading(doc, "Summary");
    writeLine(doc, `Estimate type: ${toSentenceCase(s.estimateType || "preliminary")}`);
    writeLine(doc, `Impacts: ${Array.isArray(s.impacts) ? s.impacts.join(", ") : "N/A"}`);
    writeLine(doc, `Severity: ${toSentenceCase(s.severity || "functional")}`);
    writeLine(doc, `Photos reviewed: ${s.photoCount || 0}`);
    writeLine(doc, `Confidence: ${toSentenceCase(s.confidence || "low")}`);

    writeHeading(doc, "Rates");
    const rates = report.rates || {};
    writeLine(doc, `Body labor: $${Number(rates.bodyLaborPerHour || DEFAULT_SHOP_RATES.bodyLaborPerHour).toFixed(2)} / hr`);
    writeLine(doc, `Structural labor: $${Number(rates.structuralLaborPerHour || DEFAULT_SHOP_RATES.structuralLaborPerHour).toFixed(2)} / hr`);
    writeLine(doc, `Frame labor: $${Number(rates.frameLaborPerHour || DEFAULT_SHOP_RATES.frameLaborPerHour).toFixed(2)} / hr`);
    writeLine(doc, `Mechanical labor: $${Number(rates.mechanicalLaborPerHour || DEFAULT_SHOP_RATES.mechanicalLaborPerHour).toFixed(2)} / hr`);
    writeLine(doc, `Electrical labor: $${Number(rates.electricalLaborPerHour || DEFAULT_SHOP_RATES.electricalLaborPerHour).toFixed(2)} / hr`);
    writeLine(doc, `Paint materials: $${Number(rates.paintMaterialsPerPaintHour || DEFAULT_SHOP_RATES.paintMaterialsPerPaintHour).toFixed(2)} / paint hr`);
    writeLine(doc, `Inside storage: $${Number(rates.insideStoragePerDay || DEFAULT_SHOP_RATES.insideStoragePerDay).toFixed(2)} / day`);
    writeLine(doc, `Outside storage: $${Number(rates.outsideStoragePerDay || DEFAULT_SHOP_RATES.outsideStoragePerDay).toFixed(2)} / day`);
    writeLine(doc, `Towing: $${Number(rates.towingPerMile || DEFAULT_SHOP_RATES.towingPerMile).toFixed(2)} / mile`);

    writeHeading(doc, "Output 1: Repair vs Replace");
    if (Array.isArray(calc.lineItems) && calc.lineItems.length) {
      for (const row of calc.lineItems) {
        writeLine(doc, `${row.component} | ${row.action} | ${row.laborType} | ${Number(row.laborHours || 0).toFixed(2)} hr | $${Number(row.laborTotal || 0).toFixed(2)}`);
        writeLine(doc, `  Notes: ${row.notes}`);
      }
    } else {
      for (const row of report.output1 || []) {
        writeLine(doc, `${row.component} | ${row.action} | ${row.laborBucket} | ${row.confidence}`);
        writeLine(doc, `  Notes: ${row.notes}`);
      }
    }

    writeHeading(doc, "Estimate Totals");
    writeLine(doc, `Labor subtotal: $${Number(calc.laborSubtotal || 0).toFixed(2)}`);
    writeLine(doc, `Paint materials: $${Number(calc.paintMaterialsTotal || 0).toFixed(2)}`);
    writeLine(doc, `OEM parts: $${Number(calc.partsSubtotal || 0).toFixed(2)}`);
    writeLine(doc, `Inside storage: $${Number(calc.insideStorageTotal || 0).toFixed(2)}`);
    writeLine(doc, `Outside storage: $${Number(calc.outsideStorageTotal || 0).toFixed(2)}`);
    writeLine(doc, `Towing: $${Number(calc.towingTotal || 0).toFixed(2)}`);
    writeLine(doc, `Grand total: $${Number(calc.grandTotal || 0).toFixed(2)}`);

    const parts = normalizeParts(report.parts);
    writeHeading(doc, "OEM Parts & List Pricing");
    if (parts.items.length) {
      for (const item of parts.items) {
        writeLine(
          doc,
          `${item.component || "Part"} | PN: ${item.partNumber || "N/A"} | Qty: ${Number(item.quantity || 0).toFixed(2)} | List: $${Number(item.listPrice || 0).toFixed(2)} | Line: $${Number(item.lineTotal || 0).toFixed(2)}`
        );
      }
      writeLine(doc, `Parts subtotal: $${Number(parts.subtotal || 0).toFixed(2)}`);
    } else {
      writeLine(doc, "No OEM parts returned.");
    }

    writeHeading(doc, "Output 2: Missing Ops");
    for (const row of report.output2 || []) {
      writeLine(doc, `${row.missingOperation} [${row.category}]`);
      writeLine(doc, `  Applies to: ${row.appliesTo}`);
      writeLine(doc, `  Why: ${row.why}`);
      writeLine(doc, `  Proof: ${row.bestProof} | ${row.billNowOrPending}`);
    }

    writeHeading(doc, "Output 3: Paste-Ready Notes");
    writeBullets(doc, report.output3 || []);

    writeHeading(doc, "Output 4: Red Flags / Teardown Questions");
    writeBullets(doc, report.output4 || []);

    writeHeading(doc, "Output 5: System Entry Notes");
    for (const row of report.output5 || []) {
      writeLine(doc, `- ${row.system}: ${row.note}`);
    }

    if (Array.isArray(report.assumptions) && report.assumptions.length) {
      writeHeading(doc, "Assumptions");
      writeBullets(doc, report.assumptions);
    }

    doc.end();
  });
}

app.get("/api/health", (_req, res) => {
  res.json({
    ok: true,
    hasOpenAiKey: Boolean(process.env.OPENAI_API_KEY),
    now: nowIso()
  });
});

app.post("/api/vin/decode", async (req, res) => {
  const vinRaw = req.body && req.body.vin;
  const vin = sanitizeVin(vinRaw);

  if (!isVinValidShape(vin)) {
    return res.status(400).json({
      ok: false,
      error: "VIN must be 17 characters (excluding I, O, Q)."
    });
  }

  try {
    const core = await decodeVinFromNhtsa(vin);
    const extra = await decodeVinFromExtraProvider(vin);

    return res.json({
      ok: true,
      vehicle: {
        vin: core.vin,
        year: core.year,
        make: core.make,
        model: core.model,
        trim: core.trim,
        bodyClass: core.bodyClass,
        vehicleType: core.vehicleType,
        engine: core.engine,
        fuelType: core.fuelType,
        driveType: core.driveType,
        transmission: core.transmission,
        plantCountry: core.plantCountry,
        plantCompany: core.plantCompany,
        series: core.series,
        doors: core.doors,
        antiBrakeSystem: core.antiBrakeSystem,
        paintCode: extra.paintCode,
        paintDescription: extra.paintDescription,
        paintSource: extra.source
      },
      notes: {
        paintCodeNote: extra.paintCode
          ? "Paint code returned from configured provider."
          : "Paint code not available from base VIN decode. Add OEM/provider integration or door-jamb label photo for exact paint code."
      }
    });
  } catch (error) {
    return res.status(500).json({
      ok: false,
      error: error.message || "VIN decode failed"
    });
  }
});

app.post("/api/license/extract", upload.single("license"), async (req, res) => {
  const licenseFile = req.file || null;
  if (!licenseFile) {
    return res.status(400).json({ ok: false, error: "License image is required." });
  }

  try {
    const extracted = await extractCustomerFromLicense(licenseFile);
    return res.json({
      ok: true,
      customer: extracted.customer,
      confidence: extracted.confidence,
      notes: extracted.notes,
      source: extracted.source
    });
  } catch (error) {
    return res.status(500).json({ ok: false, error: error.message || "License extraction failed" });
  }
});

app.post("/api/vehicle-label/extract", upload.single("vehicleLabel"), async (req, res) => {
  const vehicleLabelFile = req.file || null;
  if (!vehicleLabelFile) {
    return res.status(400).json({ ok: false, error: "Door-jamb label image is required." });
  }

  try {
    const extracted = await extractVehicleLabelFromImage(vehicleLabelFile);
    return res.json({
      ok: true,
      vehicleLabel: extracted.vehicleLabel,
      source: extracted.source,
      confidence: extracted.confidence,
      notes: extracted.notes
    });
  } catch (error) {
    return res.status(500).json({ ok: false, error: error.message || "Door-jamb label extraction failed" });
  }
});

app.post("/api/estimate/generate", upload.fields([
  { name: "photos", maxCount: 20 },
  { name: "license", maxCount: 1 },
  { name: "vehicleLabel", maxCount: 1 }
]), async (req, res) => {
    try {
      const body = req.body && typeof req.body === "object" ? req.body : {};
      const payload = safeJsonParse(body.payload || "{}", {}) || {};
      payload.customer = normalizeCustomer(payload.customer);
      payload.rates = normalizeShopRates(payload.rates);
      payload.charges = normalizeAdditionalCharges(payload.charges);
      payload.vehicleLabel = normalizeVehicleLabel(payload.vehicleLabel);

      const filesByField = req.files && typeof req.files === "object" ? req.files : {};
      const photos = Array.isArray(filesByField.photos) ? filesByField.photos : [];
      const licenseFile = Array.isArray(filesByField.license) && filesByField.license.length ? filesByField.license[0] : null;
      const vehicleLabelFile = Array.isArray(filesByField.vehicleLabel) && filesByField.vehicleLabel.length ? filesByField.vehicleLabel[0] : null;
      const photoCount = photos.length;

      let vehicleLabel = normalizeVehicleLabel(payload.vehicleLabel);
      const vehicleLabelNotes = [];
      let vehicleLabelSource = vehicleLabel.source || "payload";
      if (vehicleLabelFile) {
        try {
          const extractedLabel = await extractVehicleLabelFromImage(vehicleLabelFile);
          vehicleLabel = mergeVehicleLabels(vehicleLabel, extractedLabel.vehicleLabel);
          vehicleLabelSource = extractedLabel.source || "vehicle-label-ai";
          if (Array.isArray(extractedLabel.notes)) {
            for (const note of extractedLabel.notes) {
              vehicleLabelNotes.push(note);
            }
          }
        } catch (error) {
          vehicleLabelSource = "vehicle-label-error";
          vehicleLabelNotes.push(`Door-jamb extraction failed: ${error.message}`);
        }
      }
      payload.vehicleLabel = vehicleLabel;

      let extractedCustomer = normalizeCustomer({});
      let licenseNotes = [];
      let licenseSource = "none";

      if (licenseFile) {
        try {
          const extracted = await extractCustomerFromLicense(licenseFile);
          extractedCustomer = extracted.customer;
          licenseNotes = extracted.notes || [];
          licenseSource = extracted.source || "license-ai";
        } catch (error) {
          licenseNotes.push(`License extraction failed: ${error.message}`);
          licenseSource = "license-error";
        }
      }

      const mergedCustomer = mergeCustomers(payload.customer, extractedCustomer);

      const vin = sanitizeVin(payload.vin || vehicleLabel.vin || "");
      if (vin && !payload.vin) {
        payload.vin = vin;
      }
      if (payload.vin && vehicleLabel.vin && sanitizeVin(payload.vin) !== vehicleLabel.vin) {
        vehicleLabelNotes.push("VIN mismatch between typed VIN and door-jamb label; review before final billing.");
      }

      let decodedVehicle = {
        vin,
        year: String(payload.year || ""),
        make: payload.make || "",
        model: payload.model || "",
        trim: "",
        paintCode: null,
        paintDescription: null
      };

      if (vin && isVinValidShape(vin)) {
        try {
          const core = await decodeVinFromNhtsa(vin);
          const extra = await decodeVinFromExtraProvider(vin);
          decodedVehicle = {
            vin,
            year: core.year,
            make: core.make,
            model: core.model,
            trim: core.trim,
            bodyClass: core.bodyClass,
            paintCode: extra.paintCode,
            paintDescription: extra.paintDescription
          };
        } catch {
          // Keep intake fields if VIN decode fails during estimate.
        }
      }
      decodedVehicle = applyVehicleLabelToVehicle(decodedVehicle, vehicleLabel);

      if (!openaiClient || photoCount === 0) {
        const fallback = buildRuleBasedFallback(payload, decodedVehicle, photoCount, mergedCustomer, vehicleLabel);
        fallback.parts = await buildOemPartsForReport(fallback);
        applyEstimateCalculations(fallback, payload.rates, payload.charges);
        appendAssumptions(fallback, fallback.parts.assumptions);
        if (licenseSource !== "none") appendAssumptions(fallback, [`Customer data source: ${licenseSource}.`]);
        if (vehicleLabelSource && vehicleLabelSource !== "none") appendAssumptions(fallback, [`Door-jamb data source: ${vehicleLabelSource}.`]);
        appendAssumptions(fallback, licenseNotes);
        appendAssumptions(fallback, vehicleLabelNotes);
        return res.json({ ok: true, report: fallback });
      }

      try {
        const aiRaw = await generateVisionEstimate(payload, decodedVehicle, photos);
        const normalized = normalizeReport(aiRaw, payload, decodedVehicle, photoCount, mergedCustomer);
        normalized.vehicleLabel = mergeVehicleLabels(vehicleLabel, normalized.vehicleLabel);
        normalized.vehicle = applyVehicleLabelToVehicle(normalized.vehicle, normalized.vehicleLabel);
        normalized.parts = await buildOemPartsForReport(normalized);
        applyEstimateCalculations(normalized, payload.rates, payload.charges);
        appendAssumptions(normalized, normalized.parts.assumptions);
        if (licenseSource !== "none") appendAssumptions(normalized, [`Customer data source: ${licenseSource}.`]);
        if (vehicleLabelSource && vehicleLabelSource !== "none") appendAssumptions(normalized, [`Door-jamb data source: ${vehicleLabelSource}.`]);
        appendAssumptions(normalized, licenseNotes);
        appendAssumptions(normalized, vehicleLabelNotes);
        return res.json({ ok: true, report: normalized });
      } catch (error) {
        const fallback = buildRuleBasedFallback(payload, decodedVehicle, photoCount, mergedCustomer, vehicleLabel);
        fallback.parts = await buildOemPartsForReport(fallback);
        applyEstimateCalculations(fallback, payload.rates, payload.charges);
        appendAssumptions(fallback, [`AI vision generation failed: ${error.message}`]);
        appendAssumptions(fallback, fallback.parts.assumptions);
        if (licenseSource !== "none") appendAssumptions(fallback, [`Customer data source: ${licenseSource}.`]);
        if (vehicleLabelSource && vehicleLabelSource !== "none") appendAssumptions(fallback, [`Door-jamb data source: ${vehicleLabelSource}.`]);
        appendAssumptions(fallback, licenseNotes);
        appendAssumptions(fallback, vehicleLabelNotes);
        return res.json({ ok: true, report: fallback });
      }
    } catch (error) {
      return res.status(500).json({ ok: false, error: error.message || "Estimate generation crashed" });
    }
});

app.post("/api/estimate/recalculate", async (req, res) => {
  try {
    const body = req.body && typeof req.body === "object" ? req.body : {};
    const reportInput = body.report && typeof body.report === "object" ? body.report : null;
    if (!reportInput) {
      return res.status(400).json({ ok: false, error: "Missing report object for recalculate." });
    }

    const report = safeJsonParse(JSON.stringify(reportInput), {});
    report.customer = normalizeCustomer(report.customer);
    report.vehicleLabel = normalizeVehicleLabel(report.vehicleLabel);
    report.vehicle = report.vehicle && typeof report.vehicle === "object" ? report.vehicle : {};
    report.vehicle = applyVehicleLabelToVehicle(report.vehicle, report.vehicleLabel);
    report.summary = report.summary && typeof report.summary === "object" ? report.summary : {};
    report.output1 = Array.isArray(report.output1) ? report.output1 : [];
    report.output2 = Array.isArray(report.output2) ? report.output2 : [];
    report.output3 = Array.isArray(report.output3) ? report.output3 : [];
    report.output4 = Array.isArray(report.output4) ? report.output4 : [];
    report.output5 = Array.isArray(report.output5) ? report.output5 : [];
    report.assumptions = Array.isArray(report.assumptions) ? report.assumptions : [];
    report.parts = normalizeParts(report.parts);

    const rates = normalizeShopRates(body.rates || report.rates);
    const charges = normalizeAdditionalCharges(body.charges || report.charges);
    const refreshParts = Boolean(body.refreshParts);

    if (refreshParts) {
      report.parts = await buildOemPartsForReport(report);
    }

    applyEstimateCalculations(report, rates, charges);
    appendAssumptions(report, report.parts.assumptions);

    return res.json({ ok: true, report });
  } catch (error) {
    return res.status(500).json({ ok: false, error: error.message || "Estimate recalculate failed" });
  }
});

app.post("/api/report/pdf", async (req, res) => {
  const report = req.body && req.body.report;
  if (!report || typeof report !== "object") {
    return res.status(400).json({ ok: false, error: "Missing report object" });
  }

  try {
    const logoBuffer = await resolveLogoBuffer();
    const pdfBuffer = await buildPdfBuffer(report, logoBuffer);
    const vin = sanitizeVin((report.vehicle && report.vehicle.vin) || "") || "estimate";
    const filename = `collision-estimate-${vin}-${Date.now()}.pdf`;

    res.setHeader("Content-Type", "application/pdf");
    res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    return res.send(pdfBuffer);
  } catch (error) {
    return res.status(500).json({ ok: false, error: error.message || "PDF generation failed" });
  }
});

app.get("*", (req, res, next) => {
  if (req.path.startsWith("/api/")) {
    return next();
  }

  const indexPath = path.join(publicRoot, "index.html");
  if (fs.existsSync(indexPath)) {
    return res.sendFile(indexPath);
  }

  return next();
});

app.use((err, _req, res, _next) => {
  const message = err && err.message ? err.message : "Unexpected server error";
  return res.status(500).json({ ok: false, error: message });
});

if (require.main === module) {
  app.listen(port, host, () => {
    console.log(`BMB Collision Repair AI listening on http://${host}:${port}`);
    console.log(`OpenAI configured: ${Boolean(process.env.OPENAI_API_KEY)}`);
  });
}

module.exports = { app };
