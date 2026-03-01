"use strict";

const refs = {
  system: document.getElementById("system"),
  estimateType: document.getElementById("estimateType"),
  year: document.getElementById("year"),
  make: document.getElementById("make"),
  model: document.getElementById("model"),
  vin: document.getElementById("vin"),
  decodeVin: document.getElementById("decodeVin"),
  vinStatus: document.getElementById("vinStatus"),
  vehicleDecoded: document.getElementById("vehicleDecoded"),
  customerName: document.getElementById("customerName"),
  customerAddress: document.getElementById("customerAddress"),
  customerPhone: document.getElementById("customerPhone"),
  customerEmail: document.getElementById("customerEmail"),
  licensePhoto: document.getElementById("licensePhoto"),
  extractLicense: document.getElementById("extractLicense"),
  licenseStatus: document.getElementById("licenseStatus"),
  licensePreview: document.getElementById("licensePreview"),
  impactFront: document.getElementById("impactFront"),
  impactRear: document.getElementById("impactRear"),
  impactLeft: document.getElementById("impactLeft"),
  impactRight: document.getElementById("impactRight"),
  severity: document.getElementById("severity"),
  drivable: document.getElementById("drivable"),
  tAdas: document.getElementById("tAdas"),
  tLamps: document.getElementById("tLamps"),
  tWheel: document.getElementById("tWheel"),
  tBareMetal: document.getElementById("tBareMetal"),
  tCooling: document.getElementById("tCooling"),
  tWindshield: document.getElementById("tWindshield"),
  tAirbags: document.getElementById("tAirbags"),
  tNoise: document.getElementById("tNoise"),
  observedNotes: document.getElementById("observedNotes"),
  photos: document.getElementById("photos"),
  photoStats: document.getElementById("photoStats"),
  photoGrid: document.getElementById("photoGrid"),
  generate: document.getElementById("generate"),
  clearPhotos: document.getElementById("clearPhotos"),
  downloadPdf: document.getElementById("downloadPdf"),
  copyReport: document.getElementById("copyReport"),
  runStatus: document.getElementById("runStatus"),
  summary: document.getElementById("summary"),
  outputs: document.getElementById("outputs")
};

const state = {
  photos: [],
  photoUrls: [],
  licenseFile: null,
  licensePreviewUrl: "",
  decodedVehicle: null,
  extractedCustomer: null,
  lastReport: null,
  lastReportText: "",
  running: false
};

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#39;");
}

function titleCase(value) {
  return String(value || "")
    .split(" ")
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

function setRunStatus(text, isWarn) {
  refs.runStatus.textContent = text;
  refs.runStatus.classList.toggle("warn", Boolean(isWarn));
}

function setButtonBusy(button, busyText, isBusy) {
  if (isBusy) {
    button.dataset.original = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
    return;
  }

  if (button.dataset.original) {
    button.textContent = button.dataset.original;
    delete button.dataset.original;
  }
  button.disabled = false;
}

function sanitizeVin(vin) {
  return String(vin || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 17);
}

function getImpacts() {
  const selected = [];
  if (refs.impactFront.checked) selected.push("front");
  if (refs.impactRear.checked) selected.push("rear");
  if (refs.impactLeft.checked) selected.push("left");
  if (refs.impactRight.checked) selected.push("right");
  return selected.length ? selected : ["front"];
}

function inferTriggersFromNotes(noteText, triggers) {
  const text = noteText.toLowerCase();
  if (/(radar|camera|sensor|adas)/.test(text)) triggers.adas = true;
  if (/(lamp|headlamp|tail lamp|light)/.test(text)) triggers.lamps = true;
  if (/(wheel|suspension|steering|toe|camber|alignment)/.test(text)) triggers.wheel = true;
  if (/(bare metal|weld|section|seam|corrosion)/.test(text)) triggers.bareMetal = true;
  if (/(radiator|condenser|cooling|shutter|coolant)/.test(text)) triggers.cooling = true;
  if (/(windshield|mirror camera)/.test(text)) triggers.windshield = true;
  if (/(airbag|srs|seat belt|pretensioner)/.test(text)) triggers.airbags = true;
  if (/(noise|fitment|flush|gap)/.test(text)) triggers.noise = true;
}

function collectCustomer() {
  return {
    fullName: refs.customerName.value.trim(),
    address: refs.customerAddress.value.trim(),
    phone: refs.customerPhone.value.trim(),
    email: refs.customerEmail.value.trim()
  };
}

function collectInputs() {
  const notes = refs.observedNotes.value.trim();
  const triggers = {
    adas: refs.tAdas.checked,
    lamps: refs.tLamps.checked,
    wheel: refs.tWheel.checked,
    bareMetal: refs.tBareMetal.checked,
    cooling: refs.tCooling.checked,
    windshield: refs.tWindshield.checked,
    airbags: refs.tAirbags.checked,
    noise: refs.tNoise.checked
  };

  inferTriggersFromNotes(notes, triggers);

  return {
    system: refs.system.value,
    estimateType: refs.estimateType.value,
    year: Number(refs.year.value) || null,
    make: refs.make.value.trim(),
    model: refs.model.value.trim(),
    vin: sanitizeVin(refs.vin.value),
    impacts: getImpacts(),
    severity: refs.severity.value,
    drivable: refs.drivable.value,
    notes,
    triggers,
    customer: collectCustomer()
  };
}

function clearPhotoUrls() {
  for (const url of state.photoUrls) {
    URL.revokeObjectURL(url);
  }
  state.photoUrls = [];
}

function clearLicensePreview() {
  if (state.licensePreviewUrl) {
    URL.revokeObjectURL(state.licensePreviewUrl);
    state.licensePreviewUrl = "";
  }
  refs.licensePreview.className = "license-preview empty";
  refs.licensePreview.innerHTML = "No license photo selected.";
}

function updateLicensePreview() {
  clearLicensePreview();
  if (!state.licenseFile) return;

  const objectUrl = URL.createObjectURL(state.licenseFile);
  state.licensePreviewUrl = objectUrl;
  refs.licensePreview.className = "license-preview";
  refs.licensePreview.innerHTML = `<img src="${objectUrl}" alt="Driver license preview">`;
}

function updatePhotoPreview() {
  clearPhotoUrls();
  refs.photoGrid.innerHTML = "";

  if (!state.photos.length) {
    refs.photoStats.textContent = "No photos uploaded.";
    return;
  }

  refs.photoStats.textContent = `${state.photos.length} photo(s) uploaded.`;

  for (const file of state.photos) {
    const objectUrl = URL.createObjectURL(file);
    state.photoUrls.push(objectUrl);

    const card = document.createElement("article");
    card.className = "photo-card";
    card.innerHTML = `
      <img src="${objectUrl}" alt="${escapeHtml(file.name)}">
      <p>${escapeHtml(file.name)}</p>
    `;

    refs.photoGrid.appendChild(card);
  }
}

function renderDecodedVehicle(vehicle, noteText) {
  if (!vehicle) {
    refs.vehicleDecoded.className = "decoded empty";
    refs.vehicleDecoded.innerHTML = "VIN decode results will appear here.";
    return;
  }

  const paint = vehicle.paintCode
    ? `${vehicle.paintCode}${vehicle.paintDescription ? ` (${vehicle.paintDescription})` : ""}`
    : "Unknown";

  refs.vehicleDecoded.className = "decoded";
  refs.vehicleDecoded.innerHTML = `
    <div><strong>Decoded:</strong> ${escapeHtml([vehicle.year, vehicle.make, vehicle.model].filter(Boolean).join(" ") || "N/A")}</div>
    <div><strong>Trim:</strong> ${escapeHtml(vehicle.trim || "N/A")}</div>
    <div><strong>Body:</strong> ${escapeHtml(vehicle.bodyClass || vehicle.vehicleType || "N/A")}</div>
    <div><strong>Engine:</strong> ${escapeHtml(vehicle.engine || "N/A")}</div>
    <div><strong>Drive/Trans:</strong> ${escapeHtml([vehicle.driveType, vehicle.transmission].filter(Boolean).join(" / ") || "N/A")}</div>
    <div><strong>Paint code:</strong> ${escapeHtml(paint)}</div>
    <div><strong>Paint source:</strong> ${escapeHtml(vehicle.paintSource || "none")}</div>
    <div class="muted">${escapeHtml(noteText || "")}</div>
  `;
}

function applyCustomerData(customer) {
  if (!customer) return;
  if (customer.fullName) refs.customerName.value = customer.fullName;
  if (customer.address) refs.customerAddress.value = customer.address;
  if (customer.phone) refs.customerPhone.value = customer.phone;
  if (customer.email) refs.customerEmail.value = customer.email;
}

async function extractLicense() {
  if (!state.licenseFile) {
    refs.licenseStatus.textContent = "Select a driver's license photo first.";
    refs.licenseStatus.classList.add("warn");
    return;
  }

  refs.licenseStatus.classList.remove("warn");
  refs.licenseStatus.textContent = "Extracting customer info from license...";
  setButtonBusy(refs.extractLicense, "Extracting...", true);

  try {
    const formData = new FormData();
    formData.append("license", state.licenseFile, state.licenseFile.name);

    const response = await fetch("/api/license/extract", {
      method: "POST",
      body: formData
    });

    const json = await response.json();
    if (!response.ok || !json.ok) {
      throw new Error(json.error || "License extraction failed");
    }

    state.extractedCustomer = json.customer || null;
    applyCustomerData(state.extractedCustomer);

    const source = json.source || "unknown";
    refs.licenseStatus.textContent = `License extracted (${source}). Review fields before generating.`;
  } catch (error) {
    refs.licenseStatus.textContent = `License extraction failed: ${error.message}`;
    refs.licenseStatus.classList.add("warn");
  } finally {
    setButtonBusy(refs.extractLicense, "Extracting...", false);
  }
}

async function decodeVin() {
  const vin = sanitizeVin(refs.vin.value);
  refs.vin.value = vin;

  if (vin.length !== 17) {
    refs.vinStatus.textContent = "VIN must be 17 characters.";
    refs.vinStatus.classList.add("warn");
    return;
  }

  refs.vinStatus.classList.remove("warn");
  refs.vinStatus.textContent = "Decoding VIN...";
  setButtonBusy(refs.decodeVin, "Decoding...", true);

  try {
    const response = await fetch("/api/vin/decode", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ vin })
    });

    const json = await response.json();
    if (!response.ok || !json.ok) {
      throw new Error(json.error || "VIN decode failed");
    }

    const vehicle = json.vehicle;
    state.decodedVehicle = vehicle;

    if (vehicle.year) refs.year.value = String(vehicle.year);
    if (vehicle.make) refs.make.value = vehicle.make;
    if (vehicle.model) refs.model.value = vehicle.model;

    refs.vinStatus.textContent = "VIN decoded.";
    renderDecodedVehicle(vehicle, json.notes && json.notes.paintCodeNote);
  } catch (error) {
    refs.vinStatus.textContent = `VIN decode failed: ${error.message}`;
    refs.vinStatus.classList.add("warn");
    renderDecodedVehicle(null, "");
  } finally {
    setButtonBusy(refs.decodeVin, "Decoding...", false);
  }
}

function tableHtml(headers, rows) {
  const head = headers.map((value) => `<th>${escapeHtml(value)}</th>`).join("");
  const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderSummary(report) {
  const v = report.vehicle || {};
  const s = report.summary || {};
  const c = report.customer || {};

  const vehicle = [v.year, v.make, v.model].filter(Boolean).join(" ") || "Vehicle not set";
  const impacts = Array.isArray(s.impacts) ? s.impacts.join(" / ") : "N/A";
  const paint = v.paintCode || "Unknown";

  refs.summary.innerHTML = `
    <article class="metric">
      <h4>Vehicle</h4>
      <p>${escapeHtml(vehicle)}</p>
    </article>
    <article class="metric">
      <h4>VIN</h4>
      <p>${escapeHtml(v.vin || "N/A")}</p>
    </article>
    <article class="metric">
      <h4>Paint</h4>
      <p>${escapeHtml(paint)}</p>
    </article>
    <article class="metric">
      <h4>Estimate Context</h4>
      <p>${escapeHtml(titleCase([s.estimateType, impacts, s.severity].filter(Boolean).join(" | ")))}</p>
    </article>
    <article class="metric">
      <h4>Vision Source</h4>
      <p>${escapeHtml(report.source || "unknown")}</p>
    </article>
    <article class="metric">
      <h4>Photos Reviewed</h4>
      <p>${escapeHtml(String(s.photoCount || 0))}</p>
    </article>
    <article class="metric">
      <h4>Customer</h4>
      <p>${escapeHtml(c.fullName || "Not set")}</p>
    </article>
  `;
}

function renderReport(report) {
  renderSummary(report);
  const customer = report.customer || {};

  const output1Rows = (report.output1 || []).map((row) => [
    row.component,
    row.action,
    row.laborBucket,
    row.notes,
    row.confidence
  ]);

  const output2Rows = (report.output2 || []).map((row) => [
    row.missingOperation,
    row.category,
    row.appliesTo,
    row.why,
    row.bestProof,
    row.billNowOrPending
  ]);

  const output1 = output1Rows.length
    ? tableHtml(["Component/Panel", "Action", "Labor Bucket", "Notes/Triggers", "Confidence"], output1Rows)
    : "<p class=\"empty\">No repair/replace lines returned.</p>";

  const output2 = output2Rows.length
    ? tableHtml(["Missing Operation", "Category", "Applies To", "Why", "Best Proof To Attach", "Bill now or pending?"], output2Rows)
    : "<p class=\"empty\">No missing operations returned.</p>";

  const output3 = (report.output3 || []).length
    ? `<div class="notes">${report.output3.map((note) => `<article class="note">${escapeHtml(note)}</article>`).join("")}</div>`
    : "<p class=\"empty\">No paste-ready notes returned.</p>";

  const output4 = (report.output4 || []).length
    ? `<ul>${report.output4.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "<p class=\"empty\">No red flags returned.</p>";

  const output5 = (report.output5 || []).length
    ? `<ul>${report.output5.map((item) => `<li><strong>${escapeHtml(item.system)}:</strong> ${escapeHtml(item.note)}</li>`).join("")}</ul>`
    : "<p class=\"empty\">No system notes returned.</p>";

  const assumptions = (report.assumptions || []).length
    ? `<ul>${report.assumptions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "<p class=\"empty\">No additional assumptions listed.</p>";

  refs.outputs.innerHTML = `
    <section class="block">
      <h3>Customer (Supplement Header)</h3>
      <ul>
        <li><strong>Full Name:</strong> ${escapeHtml(customer.fullName || "N/A")}</li>
        <li><strong>Address:</strong> ${escapeHtml(customer.address || "N/A")}</li>
        <li><strong>Phone:</strong> ${escapeHtml(customer.phone || "N/A")}</li>
        <li><strong>Email:</strong> ${escapeHtml(customer.email || "N/A")}</li>
      </ul>
    </section>

    <section class="block">
      <h3>Output 1: Repair vs Replace Table</h3>
      ${output1}
    </section>

    <section class="block">
      <h3>Output 2: Missing Ops Table</h3>
      ${output2}
    </section>

    <section class="block">
      <h3>Output 3: Paste-Ready Justification Notes</h3>
      ${output3}
    </section>

    <section class="block">
      <h3>Output 4: Red Flags / Teardown Questions</h3>
      ${output4}
    </section>

    <section class="block">
      <h3>Output 5: System Entry Notes</h3>
      ${output5}
    </section>

    <section class="block">
      <h3>Assumptions / Evidence Gaps</h3>
      ${assumptions}
    </section>
  `;
}

function buildPlainTextReport(report) {
  const lines = [];
  const vehicle = report.vehicle || {};
  const customer = report.customer || {};
  const summary = report.summary || {};

  lines.push("BMB COLLISION REPAIR AI");
  lines.push(`Generated: ${report.generatedAt || new Date().toISOString()}`);
  lines.push(`VIN: ${vehicle.vin || "N/A"}`);
  lines.push(`Vehicle: ${[vehicle.year, vehicle.make, vehicle.model].filter(Boolean).join(" ") || "N/A"}`);
  lines.push(`Trim: ${vehicle.trim || "N/A"}`);
  lines.push(`Paint: ${vehicle.paintCode || "Unknown"} ${vehicle.paintDescription ? `(${vehicle.paintDescription})` : ""}`);
  lines.push(`Customer: ${customer.fullName || "N/A"}`);
  lines.push(`Customer address: ${customer.address || "N/A"}`);
  lines.push(`Customer phone: ${customer.phone || "N/A"}`);
  lines.push(`Customer email: ${customer.email || "N/A"}`);
  lines.push(`Source: ${report.source || "unknown"}`);
  lines.push(`Context: ${summary.estimateType || "preliminary"} | impacts: ${(summary.impacts || []).join(", ")} | severity: ${summary.severity || "functional"}`);
  lines.push("");

  lines.push("OUTPUT 1 - REPAIR VS REPLACE");
  for (const row of report.output1 || []) {
    lines.push(`- ${row.component} | ${row.action} | ${row.laborBucket} | ${row.confidence}`);
    lines.push(`  Notes: ${row.notes}`);
  }

  lines.push("");
  lines.push("OUTPUT 2 - MISSING OPS");
  for (const row of report.output2 || []) {
    lines.push(`- ${row.missingOperation} | ${row.category} | ${row.billNowOrPending}`);
    lines.push(`  Applies to: ${row.appliesTo}`);
    lines.push(`  Why: ${row.why}`);
    lines.push(`  Proof: ${row.bestProof}`);
  }

  lines.push("");
  lines.push("OUTPUT 3 - NOTES");
  for (const row of report.output3 || []) {
    lines.push(`- ${row}`);
  }

  lines.push("");
  lines.push("OUTPUT 4 - RED FLAGS");
  for (const row of report.output4 || []) {
    lines.push(`- ${row}`);
  }

  lines.push("");
  lines.push("OUTPUT 5 - SYSTEM ENTRY NOTES");
  for (const row of report.output5 || []) {
    lines.push(`- ${row.system}: ${row.note}`);
  }

  if ((report.assumptions || []).length) {
    lines.push("");
    lines.push("ASSUMPTIONS");
    for (const row of report.assumptions) {
      lines.push(`- ${row}`);
    }
  }

  return lines.join("\n");
}

function buildClientFallback(inputs) {
  const output1 = [
    {
      component: inputs.impacts.includes("rear") ? "Rear bumper cover" : "Front bumper cover",
      action: inputs.severity === "cosmetic" ? "Repair" : "Replace",
      laborBucket: "RESTORE",
      notes: "Client fallback used because server call failed.",
      confidence: "Low"
    }
  ];

  const output2 = [
    {
      missingOperation: "Pre and post scan",
      category: "Verify",
      appliesTo: "Vehicle-level",
      why: "Diagnostic baseline and post-repair confirmation.",
      bestProof: "Scan reports",
      billNowOrPending: "Bill now"
    }
  ];

  return {
    source: "client-fallback",
    generatedAt: new Date().toISOString(),
    customer: {
      fullName: (inputs.customer && inputs.customer.fullName) || "",
      address: (inputs.customer && inputs.customer.address) || "",
      phone: (inputs.customer && inputs.customer.phone) || "",
      email: (inputs.customer && inputs.customer.email) || ""
    },
    vehicle: {
      vin: inputs.vin,
      year: String(inputs.year || ""),
      make: inputs.make,
      model: inputs.model,
      trim: "",
      paintCode: state.decodedVehicle && state.decodedVehicle.paintCode ? state.decodedVehicle.paintCode : "Unknown",
      paintDescription: state.decodedVehicle && state.decodedVehicle.paintDescription ? state.decodedVehicle.paintDescription : "",
      paintConfidence: "Low"
    },
    summary: {
      estimateType: inputs.estimateType,
      impacts: inputs.impacts,
      severity: inputs.severity,
      photoCount: state.photos.length,
      confidence: "low"
    },
    output1,
    output2,
    output3: [
      "Server was unavailable, so this is a minimal local fallback output.",
      "Start the Node server to enable VIN API decode, AI photo analysis, and PDF export."
    ],
    output4: ["Re-run after server start for full estimate package."],
    output5: [
      { system: "Mitchell", note: "Use trigger-backed wording and verify included operations." },
      { system: "CCC", note: "Separate not-included support operations from base operations." },
      { system: "Audatex", note: "Keep overlap logic explicit and tied to job triggers." }
    ],
    assumptions: ["No backend response available."]
  };
}

async function generateEstimate() {
  if (state.running) return;

  state.running = true;
  setButtonBusy(refs.generate, "Generating...", true);
  setRunStatus("Analyzing photos and building estimate...", false);

  const inputs = collectInputs();

  try {
    const payload = {
      ...inputs,
      decodedVehicle: state.decodedVehicle || null
    };

    const formData = new FormData();
    formData.append("payload", JSON.stringify(payload));

    for (const file of state.photos) {
      formData.append("photos", file, file.name);
    }
    if (state.licenseFile) {
      formData.append("license", state.licenseFile, state.licenseFile.name);
    }

    const response = await fetch("/api/estimate/generate", {
      method: "POST",
      body: formData
    });

    const json = await response.json();
    if (!response.ok || !json.ok) {
      throw new Error(json.error || "Estimate generation failed");
    }

    state.lastReport = json.report;
    state.lastReportText = buildPlainTextReport(json.report);
    if (json.report && json.report.customer) {
      applyCustomerData(json.report.customer);
    }

    renderReport(json.report);

    const sourceText = json.report && json.report.source ? json.report.source : "unknown";
    setRunStatus(`Estimate complete. Source: ${sourceText}`, false);
  } catch (error) {
    const fallback = buildClientFallback(inputs);
    state.lastReport = fallback;
    state.lastReportText = buildPlainTextReport(fallback);
    renderReport(fallback);
    setRunStatus(`Estimate generated with fallback: ${error.message}`, true);
  } finally {
    setButtonBusy(refs.generate, "Generating...", false);
    state.running = false;
  }
}

async function downloadPdf() {
  if (!state.lastReport) {
    setRunStatus("Generate an estimate first, then download PDF.", true);
    return;
  }

  setButtonBusy(refs.downloadPdf, "Building PDF...", true);

  try {
    const response = await fetch("/api/report/pdf", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ report: state.lastReport })
    });

    if (!response.ok) {
      const json = await response.json().catch(() => ({}));
      throw new Error(json.error || "PDF generation failed");
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);

    const vin = sanitizeVin((state.lastReport.vehicle && state.lastReport.vehicle.vin) || "") || "estimate";
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `collision-estimate-${vin}.pdf`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();

    URL.revokeObjectURL(url);
    setRunStatus("PDF ready and downloaded.", false);
  } catch (error) {
    setRunStatus(`PDF download failed: ${error.message}`, true);
  } finally {
    setButtonBusy(refs.downloadPdf, "Building PDF...", false);
  }
}

async function copyReport() {
  if (!state.lastReportText) {
    setRunStatus("Generate an estimate first, then copy report.", true);
    return;
  }

  try {
    await navigator.clipboard.writeText(state.lastReportText);
    refs.copyReport.textContent = "Copied";
    setTimeout(() => {
      refs.copyReport.textContent = "Copy Report";
    }, 1000);
  } catch {
    setRunStatus("Clipboard copy failed. Browser permissions may block clipboard access.", true);
  }
}

refs.photos.addEventListener("change", (event) => {
  state.photos = Array.from(event.target.files || []);
  updatePhotoPreview();
});

refs.licensePhoto.addEventListener("change", (event) => {
  state.licenseFile = (event.target.files && event.target.files[0]) || null;
  updateLicensePreview();
  refs.licenseStatus.classList.remove("warn");
  refs.licenseStatus.textContent = state.licenseFile ? "License photo selected. Click Extract License to parse details." : "No license photo processed yet.";
});

refs.clearPhotos.addEventListener("click", () => {
  state.photos = [];
  refs.photos.value = "";
  updatePhotoPreview();
});

refs.decodeVin.addEventListener("click", decodeVin);
refs.extractLicense.addEventListener("click", extractLicense);
refs.generate.addEventListener("click", generateEstimate);
refs.downloadPdf.addEventListener("click", downloadPdf);
refs.copyReport.addEventListener("click", copyReport);
refs.vin.addEventListener("input", () => {
  refs.vin.value = sanitizeVin(refs.vin.value);
});

renderDecodedVehicle(null, "");
clearLicensePreview();
