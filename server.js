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

function buildPdfBuffer(report, logoBuffer, photoFiles) {
  return new Promise((resolve, reject) => {
    const doc = new PDFDocument({ size: "LETTER", margin: 24, bufferPages: true });
    const chunks = [];

    doc.on("data", (chunk) => chunks.push(chunk));
    doc.on("end", () => resolve(Buffer.concat(chunks)));
    doc.on("error", reject);

    const page = {
      left: 24,
      top: 24,
      width: 564,
      height: 744,
      right: 588,
      bottom: 768
    };

    const v = report.vehicle || {};
    const c = report.customer || {};
    const s = report.summary || {};
    const calc = report.calculation || {};
    const parts = normalizeParts(report.parts);
    const vl = normalizeVehicleLabel(report.vehicleLabel);
    const lineItems = Array.isArray(calc.lineItems) && calc.lineItems.length
      ? calc.lineItems
      : (Array.isArray(report.output1) ? report.output1.map((row) => ({
          component: row.component || "",
          action: row.action || "",
          laborType: String(row.laborBucket || "AUTO").toLowerCase(),
          laborHours: 0,
          laborTotal: 0,
          notes: row.notes || ""
        })) : []);
    const photoEntries = (Array.isArray(photoFiles) ? photoFiles : [])
      .filter((file) => file && Buffer.isBuffer(file.buffer) && String(file.mimetype || "").includes("image/"))
      .slice(0, 24);

    const normalizedForMatch = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();
    const findPartForComponent = (component) => {
      const target = normalizedForMatch(component);
      if (!target) return null;
      return parts.items.find((item) => {
        const candidate = normalizedForMatch(item.component);
        return candidate.includes(target) || target.includes(candidate);
      }) || null;
    };

    const rows = lineItems.map((row, index) => {
      const part = findPartForComponent(row.component);
      return {
        lineNumber: index + 1,
        entry: String(row.laborBucket || "AUTO"),
        labor: String(row.laborType || "body").toUpperCase().slice(0, 4),
        description: `${row.action || ""} ${row.component || ""}`.trim(),
        partType: part && part.partNumber ? part.partNumber : "Existing",
        amount: part ? Number(part.lineTotal || 0) : Number(row.laborTotal || 0),
        laborUnits: Number(row.laborHours || 0)
      };
    });

    const generatedDate = report.generatedAt ? new Date(report.generatedAt) : new Date();
    const dateText = generatedDate.toLocaleDateString("en-US");
    const timeText = generatedDate.toLocaleTimeString("en-US");
    const estimateId = `${(v.vin || "EST").slice(-6)}-${String(Math.floor(generatedDate.getTime() / 1000)).slice(-6)}`;
    const profileId = report.output5 && report.output5[0] && report.output5[0].system ? report.output5[0].system : "Mitchell";

    const setFont = (bold, size) => {
      doc.font(bold ? "Helvetica-Bold" : "Helvetica");
      doc.fontSize(size);
    };

    const drawCellText = (x, y, w, h, text, options = {}) => {
      const {
        bold = false,
        size = 7,
        align = "left",
        lineBreak = false,
        padding = 2
      } = options;
      setFont(bold, size);
      doc.text(String(text || ""), x + padding, y + 2, {
        width: Math.max(0, w - padding * 2),
        height: Math.max(0, h - 4),
        align,
        lineBreak,
        ellipsis: !lineBreak
      });
    };

    const drawLabelValueRow = (x, y, labelWidth, totalWidth, h, label, value, valueBold) => {
      doc.rect(x, y, totalWidth, h).stroke();
      doc.moveTo(x + labelWidth, y).lineTo(x + labelWidth, y + h).stroke();
      drawCellText(x, y, labelWidth, h, label, { bold: true, size: 7 });
      drawCellText(x + labelWidth, y, totalWidth - labelWidth, h, value, { bold: Boolean(valueBold), size: 7 });
    };

    const drawOuterBorder = () => {
      doc.lineWidth(0.9).rect(page.left, page.top, page.width, page.height).stroke();
      doc.lineWidth(0.6);
    };

    const drawFirstPageHeaderBlocks = () => {
      drawOuterBorder();

      const metaX = 430;
      const metaY = 24;
      const metaW = 158;
      const metaRowH = 11;
      const metaLabelW = 74;
      const metaRows = [
        ["Date", `${dateText} ${timeText}`],
        ["Estimate ID", estimateId],
        ["Est Version", "05"],
        ["Profile ID", profileId],
        ["Estimate Type", toSentenceCase(s.estimateType || "Preliminary")],
        ["Severity", toSentenceCase(s.severity || "Functional")]
      ];

      for (let index = 0; index < metaRows.length; index += 1) {
        const y = metaY + index * metaRowH;
        drawLabelValueRow(metaX, y, metaLabelW, metaW, metaRowH, metaRows[index][0], metaRows[index][1], true);
      }

      if (logoBuffer) {
        try {
          doc.image(logoBuffer, 32, 56, { fit: [120, 38], align: "left", valign: "center" });
        } catch {
          // Keep going if logo decode fails.
        }
      }

      drawLabelValueRow(162, 82, 250, 250, 16, "", "BMB RHINETRADE INC", true);
      drawLabelValueRow(162, 98, 250, 250, 14, "", "127 E Dyer Rd, Santa Ana, CA 92707", false);
      drawLabelValueRow(162, 112, 250, 250, 14, "", "Email: snopro@gmail.com", false);

      const damageX = 28;
      const damageY = 132;
      const damageW = 140;
      drawLabelValueRow(damageX, damageY, 66, damageW, 16, "Damage By", "Mike Saad", true);
      drawLabelValueRow(damageX, damageY + 16, 66, damageW, 16, "Class", "Audit", true);
      drawLabelValueRow(damageX, damageY + 36, 66, damageW, 16, "Deductible", "UNKNOWN", true);

      const vehicleX = 28;
      const vehicleY = 190;
      const vehicleW = 560;
      doc.rect(vehicleX, vehicleY, vehicleW, 122).stroke();
      doc.moveTo(vehicleX, vehicleY + 56).lineTo(vehicleX + vehicleW, vehicleY + 56).stroke();
      doc.moveTo(vehicleX, vehicleY + 72).lineTo(vehicleX + vehicleW, vehicleY + 72).stroke();

      drawLabelValueRow(vehicleX + 2, vehicleY + 2, 68, 270, 14, "Description", [v.year, v.make, v.model].filter(Boolean).join(" ") || "N/A", true);
      drawLabelValueRow(vehicleX + 272, vehicleY + 2, 90, 286, 14, "Drive Train", [v.engine, v.driveType].filter(Boolean).join(" ") || "N/A", false);
      drawLabelValueRow(vehicleX + 2, vehicleY + 16, 68, 270, 14, "Body Style", v.bodyClass || "Sedan", false);
      drawLabelValueRow(vehicleX + 272, vehicleY + 16, 90, 286, 14, "Search Code", "None", false);
      drawLabelValueRow(vehicleX + 2, vehicleY + 30, 68, 556, 14, "VIN", v.vin || "N/A", true);
      drawLabelValueRow(vehicleX + 2, vehicleY + 44, 68, 556, 14, "Paint", `${v.paintCode || "Unknown"} ${v.paintDescription ? `(${v.paintDescription})` : ""}`, false);

      const optionsTextParts = [];
      if (vl.paintCode) optionsTextParts.push(`Door Label Paint ${vl.paintCode}${vl.paintDescription ? ` (${vl.paintDescription})` : ""}`);
      if (c.fullName) optionsTextParts.push(`Customer ${c.fullName}`);
      if (c.phone) optionsTextParts.push(`Phone ${c.phone}`);
      if (Array.isArray(s.impacts) && s.impacts.length) optionsTextParts.push(`Impacts ${s.impacts.join(", ")}`);
      optionsTextParts.push(`Confidence ${s.confidence || "low"}`);
      const optionsText = optionsTextParts.join(", ") || "No additional options.";
      drawCellText(vehicleX + 4, vehicleY + 58, 64, 12, "Options:", { bold: true, size: 7 });
      drawCellText(vehicleX + 70, vehicleY + 58, vehicleW - 74, 62, optionsText, { bold: false, size: 7, lineBreak: true });
    };

    const drawLineItemTableHeader = (tableX, tableY, colWidths, headerHeight) => {
      const headers = [
        "Line\nItem",
        "Entry\nNo",
        "Labor\nType",
        "Line Item Description",
        "Part Type/\nPart Number",
        "Dollar\nAmount",
        "Labor\nUnits"
      ];
      let x = tableX;
      for (let index = 0; index < colWidths.length; index += 1) {
        const w = colWidths[index];
        doc.rect(x, tableY, w, headerHeight).stroke();
        drawCellText(x, tableY, w, headerHeight, headers[index], { bold: true, size: 6, align: "center", lineBreak: true, padding: 1 });
        x += w;
      }
    };

    const drawLineItemRows = (tableX, startY, colWidths, rowHeight, tableRows) => {
      let y = startY;
      for (const row of tableRows) {
        let x = tableX;
        const values = [
          row.lineNumber,
          row.entry,
          row.labor,
          row.description,
          row.partType,
          `$${Number(row.amount || 0).toFixed(2)}`,
          `${Number(row.laborUnits || 0).toFixed(1)}`
        ];
        for (let index = 0; index < colWidths.length; index += 1) {
          const w = colWidths[index];
          doc.rect(x, y, w, rowHeight).stroke();
          drawCellText(x, y, w, rowHeight, values[index], {
            bold: index !== 3,
            size: 7,
            align: index === 5 || index === 6 ? "right" : "left"
          });
          x += w;
        }
        y += rowHeight;
      }
      return y;
    };

    const drawFirstLinePage = (rowsForPage) => {
      drawFirstPageHeaderBlocks();
      const tableX = 24;
      const tableY = 324;
      const colWidths = [22, 42, 42, 248, 90, 62, 58];
      const headerHeight = 18;
      const rowHeight = 14;

      drawLineItemTableHeader(tableX, tableY, colWidths, headerHeight);
      drawLineItemRows(tableX, tableY + headerHeight, colWidths, rowHeight, rowsForPage);
    };

    const drawContinuationLinePage = (rowsForPage, pageIndex) => {
      drawOuterBorder();
      drawCellText(28, 34, 560, 16, `LINE ITEM CONTINUATION - PAGE ${pageIndex}`, { bold: true, size: 10, align: "center" });
      const tableX = 24;
      const tableY = 58;
      const colWidths = [22, 42, 42, 248, 90, 62, 58];
      const headerHeight = 18;
      const rowHeight = 14;
      drawLineItemTableHeader(tableX, tableY, colWidths, headerHeight);
      drawLineItemRows(tableX, tableY + headerHeight, colWidths, rowHeight, rowsForPage);
    };

    const drawSupplementPage = () => {
      doc.addPage();
      drawOuterBorder();
      drawCellText(28, 30, 560, 16, "SUPPLEMENT DETAILS / MISSING OPERATIONS", { bold: true, size: 11, align: "center" });

      const tableX = 24;
      const tableY = 56;
      const colWidths = [220, 58, 110, 74, 102];
      const headerHeight = 16;
      const rowHeight = 15;
      const headers = ["Missing Operation", "Category", "Applies To", "Bill", "Best Proof"];
      let x = tableX;
      for (let index = 0; index < colWidths.length; index += 1) {
        doc.rect(x, tableY, colWidths[index], headerHeight).stroke();
        drawCellText(x, tableY, colWidths[index], headerHeight, headers[index], { bold: true, size: 7, align: "center" });
        x += colWidths[index];
      }

      const missingRows = Array.isArray(report.output2) ? report.output2 : [];
      const maxRows = Math.min(18, missingRows.length);
      let y = tableY + headerHeight;
      for (let rowIndex = 0; rowIndex < maxRows; rowIndex += 1) {
        const row = missingRows[rowIndex] || {};
        let cx = tableX;
        const values = [
          row.missingOperation || "",
          row.category || "",
          row.appliesTo || "",
          row.billNowOrPending || "",
          row.bestProof || ""
        ];
        for (let col = 0; col < colWidths.length; col += 1) {
          doc.rect(cx, y, colWidths[col], rowHeight).stroke();
          drawCellText(cx, y, colWidths[col], rowHeight, values[col], { size: 6.8, lineBreak: false });
          cx += colWidths[col];
        }
        y += rowHeight;
      }

      const totalsX = 338;
      const totalsY = 360;
      const totalsW = 250;
      const totalsRowH = 15;
      const totals = [
        ["Labor Subtotal", `$${Number(calc.laborSubtotal || 0).toFixed(2)}`],
        ["Paint Materials", `$${Number(calc.paintMaterialsTotal || 0).toFixed(2)}`],
        ["OEM Parts", `$${Number(calc.partsSubtotal || 0).toFixed(2)}`],
        ["Inside Storage", `$${Number(calc.insideStorageTotal || 0).toFixed(2)}`],
        ["Outside Storage", `$${Number(calc.outsideStorageTotal || 0).toFixed(2)}`],
        ["Towing", `$${Number(calc.towingTotal || 0).toFixed(2)}`],
        ["Grand Total", `$${Number(calc.grandTotal || 0).toFixed(2)}`]
      ];
      for (let index = 0; index < totals.length; index += 1) {
        const rowY = totalsY + index * totalsRowH;
        drawLabelValueRow(totalsX, rowY, 140, totalsW, totalsRowH, totals[index][0], totals[index][1], true);
      }

      const notesY = 360;
      doc.rect(24, notesY, 304, 150).stroke();
      drawCellText(28, notesY + 2, 294, 12, "Justification Notes", { bold: true, size: 8 });
      let noteCursorY = notesY + 16;
      const notes = Array.isArray(report.output3) ? report.output3 : [];
      for (const note of notes.slice(0, 6)) {
        drawCellText(30, noteCursorY, 292, 18, `- ${note}`, { size: 7, lineBreak: true });
        noteCursorY += 20;
      }

      const flagsY = 516;
      doc.rect(24, flagsY, 304, 108).stroke();
      drawCellText(28, flagsY + 2, 294, 12, "Red Flags / Teardown Questions", { bold: true, size: 8 });
      let flagCursorY = flagsY + 16;
      for (const flag of (Array.isArray(report.output4) ? report.output4 : []).slice(0, 5)) {
        drawCellText(30, flagCursorY, 292, 16, `- ${flag}`, { size: 7, lineBreak: true });
        flagCursorY += 17;
      }

      doc.rect(338, 478, 250, 146).stroke();
      drawCellText(342, 482, 242, 12, "System Entry Notes", { bold: true, size: 8 });
      let sysY = 496;
      for (const item of (Array.isArray(report.output5) ? report.output5 : []).slice(0, 5)) {
        drawCellText(344, sysY, 240, 22, `- ${item.system}: ${item.note}`, { size: 7, lineBreak: true });
        sysY += 24;
      }
    };

    const drawDamagePhotoPages = () => {
      if (!photoEntries.length) return;

      const pageTitle = "DAMAGE PHOTO SHEET";
      const gridX = 24;
      const gridY = 58;
      const gridW = 560;
      const gridH = 642;
      const cols = 2;
      const rowsPerPage = 2;
      const photosPerPage = cols * rowsPerPage;
      const gapX = 12;
      const gapY = 16;
      const cellW = (gridW - gapX) / cols;
      const cellH = (gridH - gapY) / rowsPerPage;

      let cursor = 0;
      let sheetPage = 1;

      while (cursor < photoEntries.length) {
        doc.addPage();
        drawOuterBorder();
        drawCellText(28, 30, 560, 16, `${pageTitle} - PAGE ${sheetPage}`, { bold: true, size: 11, align: "center" });

        for (let index = 0; index < photosPerPage && cursor < photoEntries.length; index += 1) {
          const file = photoEntries[cursor];
          const col = index % cols;
          const row = Math.floor(index / cols);
          const cellX = gridX + col * (cellW + gapX);
          const cellY = gridY + row * (cellH + gapY);
          const captionH = 16;
          const imageBoxY = cellY;
          const imageBoxH = cellH - captionH;

          doc.rect(cellX, imageBoxY, cellW, imageBoxH).stroke();
          try {
            doc.image(file.buffer, cellX + 4, imageBoxY + 4, {
              fit: [cellW - 8, imageBoxH - 8],
              align: "center",
              valign: "center"
            });
          } catch {
            drawCellText(cellX, imageBoxY + (imageBoxH / 2) - 7, cellW, 14, "Image render failed", {
              bold: false,
              size: 7,
              align: "center"
            });
          }

          doc.rect(cellX, imageBoxY + imageBoxH, cellW, captionH).stroke();
          drawCellText(
            cellX,
            imageBoxY + imageBoxH,
            cellW,
            captionH,
            `Photo ${cursor + 1}: ${file.originalname || "damage-image"}`,
            { bold: false, size: 6.5, align: "left", lineBreak: false }
          );

          cursor += 1;
        }

        sheetPage += 1;
      }
    };

    const rowsFirstPageCapacity = 30;
    const rowsContinuationCapacity = 47;
    const firstRows = rows.slice(0, rowsFirstPageCapacity);
    drawFirstLinePage(firstRows);

    let cursor = rowsFirstPageCapacity;
    let continuationIndex = 2;
    while (cursor < rows.length) {
      doc.addPage();
      const pageRows = rows.slice(cursor, cursor + rowsContinuationCapacity);
      drawContinuationLinePage(pageRows, continuationIndex);
      cursor += rowsContinuationCapacity;
      continuationIndex += 1;
    }

    drawSupplementPage();
    drawDamagePhotoPages();

    const range = doc.bufferedPageRange();
    const totalPages = range.count;
    const recallNumber = `${dateText} ${timeText}`;
    for (let pageIndex = range.start; pageIndex < range.start + range.count; pageIndex += 1) {
      doc.switchToPage(pageIndex);
      const footerY = 748;
      doc.moveTo(24, footerY).lineTo(588, footerY).stroke();
      drawCellText(26, footerY + 2, 200, 12, `ESTIMATE RECALL NUMBER: ${recallNumber}`, { size: 6.5 });
      drawCellText(26, footerY + 12, 130, 12, "Mitchell Data Version: JUL 23", { size: 6.5 });
      drawCellText(26, footerY + 22, 100, 12, "Software Version: 7.1.243", { size: 6.5 });
      drawCellText(180, footerY + 22, 230, 12, "Copyright (C) 1994 - 2026 BMB Collision Repair AI", { size: 6.4, align: "center" });
      drawCellText(510, footerY + 22, 74, 12, `Page ${pageIndex + 1} of ${totalPages}`, { size: 6.5, align: "right" });
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

app.post("/api/report/pdf", upload.array("photos", 24), async (req, res) => {
  const body = req.body && typeof req.body === "object" ? req.body : {};
  const rawReport = typeof body.report === "string" ? safeJsonParse(body.report, null) : body.report;
  const report = rawReport && typeof rawReport === "object" ? rawReport : null;
  if (!report) {
    return res.status(400).json({ ok: false, error: "Missing report object" });
  }

  try {
    const logoBuffer = await resolveLogoBuffer();
    const photoFiles = Array.isArray(req.files) ? req.files : [];
    const pdfBuffer = await buildPdfBuffer(report, logoBuffer, photoFiles);
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
