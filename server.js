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

function buildRuleBasedFallback(payload, decodedVehicle, photoCount, customer) {
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

  const vehicle = safe.vehicle || {};
  const finalVehicle = {
    vin: vehicle.vin || decodedVehicle.vin || payload.vin || "",
    year: vehicle.year || decodedVehicle.year || String(payload.year || ""),
    make: vehicle.make || decodedVehicle.make || payload.make || "",
    model: vehicle.model || decodedVehicle.model || payload.model || "",
    trim: vehicle.trim || decodedVehicle.trim || "",
    paintCode: vehicle.paintCode || decodedVehicle.paintCode || "Unknown",
    paintDescription: vehicle.paintDescription || decodedVehicle.paintDescription || "Unknown",
    paintConfidence: vehicle.paintConfidence || (decodedVehicle.paintCode ? "High" : "Low")
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
    writeLine(doc, `VIN: ${v.vin || "N/A"}`);
    writeLine(doc, `Year/Make/Model: ${[v.year, v.make, v.model].filter(Boolean).join(" ") || "N/A"}`);
    writeLine(doc, `Trim: ${v.trim || "N/A"}`);
    writeLine(doc, `Paint: ${v.paintCode || "Unknown"} ${v.paintDescription ? `(${v.paintDescription})` : ""}`);

    writeHeading(doc, "Customer");
    const c = report.customer || {};
    writeLine(doc, `Full name: ${c.fullName || "N/A"}`);
    writeLine(doc, `Address: ${c.address || "N/A"}`);
    writeLine(doc, `Phone: ${c.phone || "N/A"}`);
    writeLine(doc, `Email: ${c.email || "N/A"}`);

    const s = report.summary || {};
    writeHeading(doc, "Summary");
    writeLine(doc, `Estimate type: ${toSentenceCase(s.estimateType || "preliminary")}`);
    writeLine(doc, `Impacts: ${Array.isArray(s.impacts) ? s.impacts.join(", ") : "N/A"}`);
    writeLine(doc, `Severity: ${toSentenceCase(s.severity || "functional")}`);
    writeLine(doc, `Photos reviewed: ${s.photoCount || 0}`);
    writeLine(doc, `Confidence: ${toSentenceCase(s.confidence || "low")}`);

    writeHeading(doc, "Output 1: Repair vs Replace");
    for (const row of report.output1 || []) {
      writeLine(doc, `${row.component} | ${row.action} | ${row.laborBucket} | ${row.confidence}`);
      writeLine(doc, `  Notes: ${row.notes}`);
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

app.post("/api/estimate/generate", upload.fields([
  { name: "photos", maxCount: 20 },
  { name: "license", maxCount: 1 }
]), async (req, res) => {
  const payload = safeJsonParse(req.body.payload, {}) || {};
  payload.customer = normalizeCustomer(payload.customer);

  const filesByField = req.files && typeof req.files === "object" ? req.files : {};
  const photos = Array.isArray(filesByField.photos) ? filesByField.photos : [];
  const licenseFile = Array.isArray(filesByField.license) && filesByField.license.length ? filesByField.license[0] : null;
  const photoCount = photos.length;
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

  const vin = sanitizeVin(payload.vin || "");

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

  if (!openaiClient || photoCount === 0) {
    const fallback = buildRuleBasedFallback(payload, decodedVehicle, photoCount, mergedCustomer);
    if (licenseSource !== "none") {
      fallback.assumptions.push(`Customer data source: ${licenseSource}.`);
      for (const note of licenseNotes) {
        fallback.assumptions.push(note);
      }
    }
    return res.json({ ok: true, report: fallback });
  }

  try {
    const aiRaw = await generateVisionEstimate(payload, decodedVehicle, photos);
    const normalized = normalizeReport(aiRaw, payload, decodedVehicle, photoCount, mergedCustomer);
    if (licenseSource !== "none") {
      normalized.assumptions = Array.isArray(normalized.assumptions) ? normalized.assumptions : [];
      normalized.assumptions.push(`Customer data source: ${licenseSource}.`);
      for (const note of licenseNotes) {
        normalized.assumptions.push(note);
      }
    }
    return res.json({ ok: true, report: normalized });
  } catch (error) {
    const fallback = buildRuleBasedFallback(payload, decodedVehicle, photoCount, mergedCustomer);
    fallback.assumptions.push(`AI vision generation failed: ${error.message}`);
    if (licenseSource !== "none") {
      fallback.assumptions.push(`Customer data source: ${licenseSource}.`);
      for (const note of licenseNotes) {
        fallback.assumptions.push(note);
      }
    }
    return res.json({ ok: true, report: fallback });
  }
});

app.post("/api/report/pdf", async (req, res) => {
  const report = req.body && req.body.report;
  if (!report || typeof report !== "object") {
    return res.status(400).json({ ok: false, error: "Missing report object" });
  }

  try {
    const pdfBuffer = await buildPdfBuffer(report);
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

if (require.main === module) {
  app.listen(port, host, () => {
    console.log(`BMB Collision Repair AI listening on http://${host}:${port}`);
    console.log(`OpenAI configured: ${Boolean(process.env.OPENAI_API_KEY)}`);
  });
}

module.exports = { app };
