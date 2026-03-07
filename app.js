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
  vehicleLabelPhoto: document.getElementById("vehicleLabelPhoto"),
  extractVehicleLabel: document.getElementById("extractVehicleLabel"),
  vehicleLabelStatus: document.getElementById("vehicleLabelStatus"),
  vehicleLabelPreview: document.getElementById("vehicleLabelPreview"),
  vehicleDecoded: document.getElementById("vehicleDecoded"),
  customerName: document.getElementById("customerName"),
  customerAddress: document.getElementById("customerAddress"),
  customerPhone: document.getElementById("customerPhone"),
  customerEmail: document.getElementById("customerEmail"),
  licensePhoto: document.getElementById("licensePhoto"),
  extractLicense: document.getElementById("extractLicense"),
  licenseStatus: document.getElementById("licenseStatus"),
  licensePreview: document.getElementById("licensePreview"),
  rateBody: document.getElementById("rateBody"),
  rateStructural: document.getElementById("rateStructural"),
  rateFrame: document.getElementById("rateFrame"),
  rateMechanical: document.getElementById("rateMechanical"),
  rateElectrical: document.getElementById("rateElectrical"),
  ratePaintMaterials: document.getElementById("ratePaintMaterials"),
  rateInsideStorage: document.getElementById("rateInsideStorage"),
  rateOutsideStorage: document.getElementById("rateOutsideStorage"),
  rateTowing: document.getElementById("rateTowing"),
  chargeInsideStorageDays: document.getElementById("chargeInsideStorageDays"),
  chargeOutsideStorageDays: document.getElementById("chargeOutsideStorageDays"),
  chargeTowingMiles: document.getElementById("chargeTowingMiles"),
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
  talkToggle: document.getElementById("talkToggle"),
  talkStatus: document.getElementById("talkStatus"),
  talkHeard: document.getElementById("talkHeard"),
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
  vehicleLabelFile: null,
  vehicleLabelPreviewUrl: "",
  extractedVehicleLabel: null,
  licenseFile: null,
  licensePreviewUrl: "",
  decodedVehicle: null,
  extractedCustomer: null,
  lastReport: null,
  lastReportText: "",
  running: false,
  voiceRecognition: null,
  voiceListening: false,
  voiceShouldListen: false,
  voiceSyncBusy: false,
  voiceQueue: Promise.resolve()
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

function asNonNegativeNumber(value, fallback) {
  const num = Number(value);
  if (!Number.isFinite(num) || num < 0) return fallback;
  return num;
}

function collectRates() {
  return {
    bodyLaborPerHour: asNonNegativeNumber(refs.rateBody.value, 83),
    structuralLaborPerHour: asNonNegativeNumber(refs.rateStructural.value, 83),
    frameLaborPerHour: asNonNegativeNumber(refs.rateFrame.value, 135),
    mechanicalLaborPerHour: asNonNegativeNumber(refs.rateMechanical.value, 175),
    electricalLaborPerHour: asNonNegativeNumber(refs.rateElectrical.value, 150),
    paintMaterialsPerPaintHour: asNonNegativeNumber(refs.ratePaintMaterials.value, 46),
    insideStoragePerDay: asNonNegativeNumber(refs.rateInsideStorage.value, 180),
    outsideStoragePerDay: asNonNegativeNumber(refs.rateOutsideStorage.value, 180),
    towingPerMile: asNonNegativeNumber(refs.rateTowing.value, 12)
  };
}

function collectCharges() {
  return {
    insideStorageDays: asNonNegativeNumber(refs.chargeInsideStorageDays.value, 0),
    outsideStorageDays: asNonNegativeNumber(refs.chargeOutsideStorageDays.value, 0),
    towingMiles: asNonNegativeNumber(refs.chargeTowingMiles.value, 0)
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
    customer: collectCustomer(),
    rates: collectRates(),
    charges: collectCharges(),
    vehicleLabel: state.extractedVehicleLabel || {}
  };
}

function setTalkStatus(text, isWarn) {
  refs.talkStatus.textContent = text;
  refs.talkStatus.classList.toggle("warn", Boolean(isWarn));
}

function setTalkHeard(text) {
  refs.talkHeard.textContent = text;
}

function parseNumericFromSpeech(text) {
  const match = String(text || "").match(/-?\d+(?:\.\d+)?/);
  if (!match) return null;
  const value = Number(match[0]);
  if (!Number.isFinite(value)) return null;
  return value;
}

function normalizeSpokenEmail(rawValue) {
  return String(rawValue || "")
    .trim()
    .toLowerCase()
    .replace(/\s+at\s+/g, "@")
    .replace(/\s+dot\s+/g, ".")
    .replace(/\s+/g, "");
}

function normalizeComponentKey(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9 ]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeActionFromSpeech(rawAction) {
  const value = String(rawAction || "").trim().toLowerCase();
  if (value === "r&i" || value === "r and i" || value === "r & i") return "R&I";
  if (value === "inspect") return "Inspect - Pending teardown";
  if (value === "replace") return "Replace";
  if (value === "repair") return "Repair";
  return titleCase(rawAction);
}

function laborBucketForAction(action) {
  const value = String(action || "").toLowerCase();
  if (value.includes("inspect")) return "VERIFY";
  if (value.includes("r&i") || value.includes("r&r")) return "ACCESS";
  return "RESTORE";
}

function ensureEditableReport() {
  if (!state.lastReport || typeof state.lastReport !== "object") return null;
  if (!Array.isArray(state.lastReport.output1)) state.lastReport.output1 = [];
  if (!state.lastReport.customer || typeof state.lastReport.customer !== "object") state.lastReport.customer = {};
  if (!state.lastReport.summary || typeof state.lastReport.summary !== "object") state.lastReport.summary = {};
  if (!state.lastReport.vehicle || typeof state.lastReport.vehicle !== "object") state.lastReport.vehicle = {};
  return state.lastReport;
}

function findOutput1LineIndex(report, componentPhrase) {
  if (!report || !Array.isArray(report.output1)) return -1;
  const target = normalizeComponentKey(componentPhrase);
  if (!target) return -1;

  return report.output1.findIndex((row) => {
    const candidate = normalizeComponentKey(row.component || "");
    return candidate.includes(target) || target.includes(candidate);
  });
}

function setInputNumericValue(inputRef, value) {
  if (!inputRef || !Number.isFinite(value) || value < 0) return false;
  inputRef.value = String(value);
  return true;
}

function syncReportHeaderFromInputs(report) {
  if (!report || typeof report !== "object") return;
  report.customer = collectCustomer();
  report.summary = report.summary || {};
  report.summary.severity = refs.severity.value;
  report.summary.estimateType = refs.estimateType.value;
  report.summary.impacts = getImpacts();
  report.vehicle = report.vehicle || {};
  report.vehicle.vin = sanitizeVin(refs.vin.value) || report.vehicle.vin || "";
  report.vehicle.year = String(refs.year.value || report.vehicle.year || "");
  report.vehicle.make = refs.make.value.trim() || report.vehicle.make || "";
  report.vehicle.model = refs.model.value.trim() || report.vehicle.model || "";
}

function applyVoiceCommand(transcriptRaw) {
  const spoken = String(transcriptRaw || "").trim();
  const lower = spoken.toLowerCase();
  if (!spoken) {
    return { handled: false, message: "No command detected.", changed: false, recalc: false, refreshParts: false };
  }

  if (/^(generate|regenerate|update)\s+(estimate|report)/.test(lower)) {
    generateEstimate();
    return { handled: true, message: "Generating estimate now.", changed: false, recalc: false, refreshParts: false };
  }

  if (/^decode vin/.test(lower)) {
    decodeVin();
    return { handled: true, message: "Decoding VIN now.", changed: false, recalc: false, refreshParts: false };
  }

  const number = parseNumericFromSpeech(lower);

  const chargeCommands = [
    { regex: /inside storage days?/, input: refs.chargeInsideStorageDays, label: "Inside storage days" },
    { regex: /outside storage days?/, input: refs.chargeOutsideStorageDays, label: "Outside storage days" },
    { regex: /towing miles?/, input: refs.chargeTowingMiles, label: "Towing miles" }
  ];
  for (const item of chargeCommands) {
    if (item.regex.test(lower)) {
      if (number == null) {
        return { handled: true, message: `Say a number for ${item.label.toLowerCase()}.`, changed: false, recalc: false, refreshParts: false, warn: true };
      }
      setInputNumericValue(item.input, number);
      return { handled: true, message: `${item.label} set to ${number}.`, changed: true, recalc: true, refreshParts: false };
    }
  }

  const rateCommands = [
    { regex: /(paint and body|body labor|body rate)/, input: refs.rateBody, label: "Body labor rate" },
    { regex: /(structural labor|structural rate)/, input: refs.rateStructural, label: "Structural labor rate" },
    { regex: /(frame straightening|frame labor|frame rate)/, input: refs.rateFrame, label: "Frame labor rate" },
    { regex: /(mechanical labor|mechanical rate)/, input: refs.rateMechanical, label: "Mechanical labor rate" },
    { regex: /(electrical labor|electrical rate)/, input: refs.rateElectrical, label: "Electrical labor rate" },
    { regex: /(paint materials|materials rate)/, input: refs.ratePaintMaterials, label: "Paint materials rate" },
    { regex: /(inside storage (rate|per day|dollars per day))/, input: refs.rateInsideStorage, label: "Inside storage rate" },
    { regex: /(outside storage (rate|per day|dollars per day))/, input: refs.rateOutsideStorage, label: "Outside storage rate" },
    { regex: /(towing (rate|per mile|dollars per mile))/, input: refs.rateTowing, label: "Towing rate" }
  ];
  for (const item of rateCommands) {
    if (item.regex.test(lower)) {
      if (number == null) {
        return { handled: true, message: `Say a number for ${item.label.toLowerCase()}.`, changed: false, recalc: false, refreshParts: false, warn: true };
      }
      setInputNumericValue(item.input, number);
      return { handled: true, message: `${item.label} set to ${number}.`, changed: true, recalc: true, refreshParts: false };
    }
  }

  const severityByPhrase = [
    { regex: /severity.*cosmetic|cosmetic severity/, value: "cosmetic", label: "Cosmetic" },
    { regex: /severity.*functional|functional severity/, value: "functional", label: "Functional" },
    { regex: /severity.*structural|structural severity/, value: "structural", label: "Structural-suspect" },
    { regex: /severity.*(srs|airbag)|(?:srs|airbag) severity/, value: "srs", label: "Airbag / SRS-involved" }
  ];
  for (const item of severityByPhrase) {
    if (item.regex.test(lower)) {
      refs.severity.value = item.value;
      return { handled: true, message: `Severity set to ${item.label}.`, changed: true, recalc: true, refreshParts: false };
    }
  }

  const estimateTypeByPhrase = [
    { regex: /estimate type.*preliminary|set preliminary/, value: "preliminary", label: "Preliminary" },
    { regex: /estimate type.*supplement|set supplement/, value: "supplement", label: "Supplement" },
    { regex: /estimate type.*final|set final/, value: "final", label: "Final" }
  ];
  for (const item of estimateTypeByPhrase) {
    if (item.regex.test(lower)) {
      refs.estimateType.value = item.value;
      return { handled: true, message: `Estimate type set to ${item.label}.`, changed: true, recalc: true, refreshParts: false };
    }
  }

  const nameMatch = spoken.match(/(?:set\s+)?customer(?:\s+full)?\s*name\s+(.+)$/i);
  if (nameMatch) {
    refs.customerName.value = titleCase(nameMatch[1].trim());
    return { handled: true, message: "Customer name updated.", changed: true, recalc: false, refreshParts: false };
  }

  const addressMatch = spoken.match(/(?:set\s+)?(?:customer\s+)?address\s+(.+)$/i);
  if (addressMatch) {
    refs.customerAddress.value = addressMatch[1].trim();
    return { handled: true, message: "Customer address updated.", changed: true, recalc: false, refreshParts: false };
  }

  const phoneMatch = spoken.match(/(?:set\s+)?(?:customer\s+)?phone(?:\s+number)?\s+(.+)$/i);
  if (phoneMatch) {
    refs.customerPhone.value = phoneMatch[1].trim();
    return { handled: true, message: "Customer phone updated.", changed: true, recalc: false, refreshParts: false };
  }

  const emailMatch = spoken.match(/(?:set\s+)?(?:customer\s+)?email\s+(.+)$/i);
  if (emailMatch) {
    refs.customerEmail.value = normalizeSpokenEmail(emailMatch[1]);
    return { handled: true, message: "Customer email updated.", changed: true, recalc: false, refreshParts: false };
  }

  const noteMatch = spoken.match(/^(?:add\s+)?note\s+(.+)$/i);
  if (noteMatch) {
    const value = noteMatch[1].trim();
    refs.observedNotes.value = refs.observedNotes.value ? `${refs.observedNotes.value}\n${value}` : value;
    return { handled: true, message: "Observed note added.", changed: true, recalc: false, refreshParts: false };
  }

  const report = ensureEditableReport();

  const removeMatch = spoken.match(/^(?:remove|delete)\s+(?:line|item)?\s*(.+)$/i);
  if (removeMatch) {
    if (!report) {
      return { handled: true, message: "Generate an estimate first, then remove a line by voice.", changed: false, recalc: false, refreshParts: false, warn: true };
    }
    const componentPhrase = removeMatch[1].trim();
    const lineIndex = findOutput1LineIndex(report, componentPhrase);
    if (lineIndex === -1) {
      return { handled: true, message: `No line matched "${componentPhrase}".`, changed: false, recalc: false, refreshParts: false, warn: true };
    }
    report.output1.splice(lineIndex, 1);
    return { handled: true, message: `Removed line for ${componentPhrase}.`, changed: true, recalc: true, refreshParts: true };
  }

  const actionMatch = spoken.match(/^(?:add\s+(?:line|item)\s+)?(replace|repair|inspect|r&i|r and i|r & i)\s+(.+)$/i);
  if (actionMatch) {
    if (!report) {
      return { handled: true, message: "Generate an estimate first, then edit lines by voice.", changed: false, recalc: false, refreshParts: false, warn: true };
    }
    const action = normalizeActionFromSpeech(actionMatch[1]);
    const componentPhrase = actionMatch[2].trim();
    const lineIndex = findOutput1LineIndex(report, componentPhrase);
    if (lineIndex >= 0) {
      report.output1[lineIndex].action = action;
      report.output1[lineIndex].laborBucket = laborBucketForAction(action);
      report.output1[lineIndex].notes = "Updated by voice command.";
      return { handled: true, message: `Updated ${report.output1[lineIndex].component} to ${action}.`, changed: true, recalc: true, refreshParts: true };
    }

    report.output1.push({
      component: titleCase(componentPhrase),
      action,
      laborBucket: laborBucketForAction(action),
      notes: "Added by voice command.",
      confidence: "Med"
    });
    return { handled: true, message: `Added line: ${action} ${componentPhrase}.`, changed: true, recalc: true, refreshParts: true };
  }

  return {
    handled: false,
    message: "Voice command not recognized. Try: set body labor to 95, inside storage days 3, replace front bumper cover.",
    changed: false,
    recalc: false,
    refreshParts: false,
    warn: true
  };
}

async function recalculateCurrentReport(refreshParts) {
  if (!state.lastReport) return;

  syncReportHeaderFromInputs(state.lastReport);

  const response = await fetch("/api/estimate/recalculate", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      report: state.lastReport,
      rates: collectRates(),
      charges: collectCharges(),
      refreshParts: Boolean(refreshParts)
    })
  });

  const json = await response.json();
  if (!response.ok || !json.ok) {
    throw new Error(json.error || "Realtime recalc failed");
  }

  state.lastReport = json.report;
  state.lastReportText = buildPlainTextReport(json.report);
  renderReport(json.report);
}

async function processVoiceTranscript(transcript) {
  setTalkHeard(`Last command: ${transcript}`);
  const outcome = applyVoiceCommand(transcript);

  if (!outcome.handled) {
    setTalkStatus(outcome.message, true);
    return;
  }

  if (!outcome.changed) {
    setTalkStatus(outcome.message, Boolean(outcome.warn));
    return;
  }

  if (state.lastReport && outcome.recalc) {
    try {
      await recalculateCurrentReport(outcome.refreshParts);
      setRunStatus(`Voice update applied: ${outcome.message}`, false);
      setTalkStatus(outcome.message, false);
      return;
    } catch (error) {
      setTalkStatus(`Voice update failed: ${error.message}`, true);
      setRunStatus(`Voice update failed: ${error.message}`, true);
      return;
    }
  }

  if (state.lastReport) {
    syncReportHeaderFromInputs(state.lastReport);
    state.lastReportText = buildPlainTextReport(state.lastReport);
    renderReport(state.lastReport);
  }

  setTalkStatus(outcome.message, false);
}

function queueVoiceTranscript(transcript) {
  state.voiceQueue = state.voiceQueue
    .then(() => processVoiceTranscript(transcript))
    .catch((error) => {
      setTalkStatus(`Voice command error: ${error.message}`, true);
    });
}

function applyListeningUi(listening) {
  refs.talkToggle.classList.toggle("listening", listening);
  refs.talkToggle.textContent = listening ? "Stop Talk Edit" : "Start Talk Edit";
}

function initVoiceRecognition() {
  const SpeechRecognitionApi = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognitionApi) {
    refs.talkToggle.disabled = true;
    setTalkStatus("Voice recognition is not supported in this browser.", true);
    return;
  }

  const recognition = new SpeechRecognitionApi();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.continuous = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    state.voiceListening = true;
    applyListeningUi(true);
    setTalkStatus("Listening... speak a command.", false);
  };

  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      const transcript = result && result[0] ? String(result[0].transcript || "").trim() : "";
      if (!transcript) continue;

      if (result.isFinal) {
        queueVoiceTranscript(transcript);
      } else {
        interim = transcript;
      }
    }

    if (interim) {
      setTalkHeard(`Hearing: ${interim}`);
    }
  };

  recognition.onerror = (event) => {
    const error = event && event.error ? event.error : "unknown";
    if (error !== "no-speech") {
      setTalkStatus(`Voice error: ${error}`, true);
    }
  };

  recognition.onend = () => {
    state.voiceListening = false;
    applyListeningUi(false);

    if (state.voiceShouldListen) {
      setTimeout(() => {
        if (!state.voiceShouldListen) return;
        try {
          recognition.start();
        } catch {
          // Browser can throw if restarting too quickly.
        }
      }, 240);
    } else {
      setTalkStatus("Voice control idle.", false);
    }
  };

  state.voiceRecognition = recognition;
}

function toggleVoiceRecognition() {
  if (!state.voiceRecognition) {
    setTalkStatus("Voice recognition is not available.", true);
    return;
  }

  if (state.voiceListening || state.voiceShouldListen) {
    state.voiceShouldListen = false;
    try {
      state.voiceRecognition.stop();
    } catch {
      // Ignore stop errors.
    }
    return;
  }

  state.voiceShouldListen = true;
  try {
    state.voiceRecognition.start();
  } catch (error) {
    state.voiceShouldListen = false;
    setTalkStatus(`Could not start voice recognition: ${error.message}`, true);
  }
}

function clearPhotoUrls() {
  for (const url of state.photoUrls) {
    URL.revokeObjectURL(url);
  }
  state.photoUrls = [];
}

function clearVehicleLabelPreview() {
  if (state.vehicleLabelPreviewUrl) {
    URL.revokeObjectURL(state.vehicleLabelPreviewUrl);
    state.vehicleLabelPreviewUrl = "";
  }
  refs.vehicleLabelPreview.className = "license-preview empty";
  refs.vehicleLabelPreview.innerHTML = "No door-jamb label selected.";
}

function updateVehicleLabelPreview() {
  clearVehicleLabelPreview();
  if (!state.vehicleLabelFile) return;

  const objectUrl = URL.createObjectURL(state.vehicleLabelFile);
  state.vehicleLabelPreviewUrl = objectUrl;
  refs.vehicleLabelPreview.className = "license-preview";
  refs.vehicleLabelPreview.innerHTML = `<img src="${objectUrl}" alt="Door-jamb label preview">`;
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

function applyVehicleLabelData(labelData) {
  if (!labelData) return;
  if (labelData.vin) refs.vin.value = sanitizeVin(labelData.vin);

  if (!state.decodedVehicle) state.decodedVehicle = {};
  if (labelData.paintCode) state.decodedVehicle.paintCode = labelData.paintCode;
  if (labelData.paintDescription) state.decodedVehicle.paintDescription = labelData.paintDescription;
  if (labelData.vin) state.decodedVehicle.vin = sanitizeVin(labelData.vin);
  state.decodedVehicle.paintSource = "door-jamb-label";

  renderDecodedVehicle(state.decodedVehicle, "Door-jamb label values applied.");
}

async function extractVehicleLabel() {
  if (!state.vehicleLabelFile) {
    refs.vehicleLabelStatus.textContent = "Select a door-jamb label photo first.";
    refs.vehicleLabelStatus.classList.add("warn");
    return;
  }

  refs.vehicleLabelStatus.classList.remove("warn");
  refs.vehicleLabelStatus.textContent = "Extracting VIN/paint from door-jamb label...";
  setButtonBusy(refs.extractVehicleLabel, "Extracting...", true);

  try {
    const formData = new FormData();
    formData.append("vehicleLabel", state.vehicleLabelFile, state.vehicleLabelFile.name);

    const response = await fetch("/api/vehicle-label/extract", {
      method: "POST",
      body: formData
    });

    const json = await response.json();
    if (!response.ok || !json.ok) {
      throw new Error(json.error || "Door-jamb label extraction failed");
    }

    state.extractedVehicleLabel = json.vehicleLabel || null;
    applyVehicleLabelData(state.extractedVehicleLabel);

    const vin = state.extractedVehicleLabel && state.extractedVehicleLabel.vin ? sanitizeVin(state.extractedVehicleLabel.vin) : "N/A";
    const paint = state.extractedVehicleLabel && state.extractedVehicleLabel.paintCode ? state.extractedVehicleLabel.paintCode : "Unknown";
    refs.vehicleLabelStatus.textContent = `Door-jamb extracted. VIN: ${vin}, Paint: ${paint}`;

    if (vin !== "N/A" && vin.length === 17) {
      await decodeVin();
    }
  } catch (error) {
    refs.vehicleLabelStatus.textContent = `Door-jamb extraction failed: ${error.message}`;
    refs.vehicleLabelStatus.classList.add("warn");
  } finally {
    setButtonBusy(refs.extractVehicleLabel, "Extracting...", false);
  }
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
  const calc = report.calculation || {};
  const grandTotal = Number(calc.grandTotal || 0);

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
    <article class="metric">
      <h4>Estimate Total</h4>
      <p>$${escapeHtml(grandTotal.toFixed(2))}</p>
    </article>
  `;
}

function renderReport(report) {
  renderSummary(report);
  const customer = report.customer || {};
  const calc = report.calculation || {};
  const parts = report.parts || {};
  const partItems = Array.isArray(parts.items) ? parts.items : [];
  const laborByType = calc.laborByType || {};
  const lineItems = Array.isArray(calc.lineItems) ? calc.lineItems : [];

  const output1Rows = lineItems.length
    ? lineItems.map((row) => [
      row.component,
      row.action,
      row.laborType,
      Number(row.laborHours || 0).toFixed(2),
      `$${Number(row.ratePerHour || 0).toFixed(2)}`,
      `$${Number(row.laborTotal || 0).toFixed(2)}`,
      Number(row.paintHours || 0).toFixed(2),
      row.notes || ""
    ])
    : (report.output1 || []).map((row) => [
      row.component,
      row.action,
      row.laborBucket,
      "-",
      "-",
      "-",
      "-",
      row.notes
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
    ? tableHtml(["Component/Panel", "Action", "Labor Type", "Hours", "Rate", "Labor Total", "Paint Hrs", "Notes/Triggers"], output1Rows)
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

  const partsTable = partItems.length
    ? tableHtml(
      ["Component", "Part Number", "Description", "Qty", "List Price", "Line Total", "Source"],
      partItems.map((item) => [
        item.component || "",
        item.partNumber || "",
        item.description || "",
        Number(item.quantity || 0).toFixed(2),
        `$${Number(item.listPrice || 0).toFixed(2)}`,
        `$${Number(item.lineTotal || 0).toFixed(2)}`,
        item.source || ""
      ])
    )
    : "<p class=\"empty\">No OEM parts returned. Configure OEM parts provider for live part numbers/list pricing.</p>";

  const totalsRows = [
    ["Body Labor", Number((laborByType.body && laborByType.body.hours) || 0).toFixed(2), `$${Number((laborByType.body && laborByType.body.rate) || 0).toFixed(2)}`, `$${Number((laborByType.body && laborByType.body.total) || 0).toFixed(2)}`],
    ["Structural Labor", Number((laborByType.structural && laborByType.structural.hours) || 0).toFixed(2), `$${Number((laborByType.structural && laborByType.structural.rate) || 0).toFixed(2)}`, `$${Number((laborByType.structural && laborByType.structural.total) || 0).toFixed(2)}`],
    ["Frame Labor", Number((laborByType.frame && laborByType.frame.hours) || 0).toFixed(2), `$${Number((laborByType.frame && laborByType.frame.rate) || 0).toFixed(2)}`, `$${Number((laborByType.frame && laborByType.frame.total) || 0).toFixed(2)}`],
    ["Mechanical Labor", Number((laborByType.mechanical && laborByType.mechanical.hours) || 0).toFixed(2), `$${Number((laborByType.mechanical && laborByType.mechanical.rate) || 0).toFixed(2)}`, `$${Number((laborByType.mechanical && laborByType.mechanical.total) || 0).toFixed(2)}`],
    ["Electrical Labor", Number((laborByType.electrical && laborByType.electrical.hours) || 0).toFixed(2), `$${Number((laborByType.electrical && laborByType.electrical.rate) || 0).toFixed(2)}`, `$${Number((laborByType.electrical && laborByType.electrical.total) || 0).toFixed(2)}`],
    ["Paint Labor", Number((laborByType.paint && laborByType.paint.hours) || 0).toFixed(2), `$${Number((laborByType.paint && laborByType.paint.rate) || 0).toFixed(2)}`, `$${Number((laborByType.paint && laborByType.paint.total) || 0).toFixed(2)}`],
    ["Paint Materials", "-", "-", `$${Number(calc.paintMaterialsTotal || 0).toFixed(2)}`],
    ["OEM Parts", "-", "-", `$${Number(calc.partsSubtotal || 0).toFixed(2)}`],
    ["Inside Storage", `${Number((calc.charges && calc.charges.insideStorageDays) || 0).toFixed(2)} day`, `$${Number((calc.rates && calc.rates.insideStoragePerDay) || 0).toFixed(2)}`, `$${Number(calc.insideStorageTotal || 0).toFixed(2)}`],
    ["Outside Storage", `${Number((calc.charges && calc.charges.outsideStorageDays) || 0).toFixed(2)} day`, `$${Number((calc.rates && calc.rates.outsideStoragePerDay) || 0).toFixed(2)}`, `$${Number(calc.outsideStorageTotal || 0).toFixed(2)}`],
    ["Towing", `${Number((calc.charges && calc.charges.towingMiles) || 0).toFixed(2)} mi`, `$${Number((calc.rates && calc.rates.towingPerMile) || 0).toFixed(2)}`, `$${Number(calc.towingTotal || 0).toFixed(2)}`],
    ["Grand Total", "-", "-", `$${Number(calc.grandTotal || 0).toFixed(2)}`]
  ];

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
      <h3>OEM Parts & List Pricing</h3>
      ${partsTable}
      <p class="muted">Parts subtotal: $${escapeHtml(Number(parts.subtotal || 0).toFixed(2))}</p>
    </section>

    <section class="block">
      <h3>Estimate Totals</h3>
      ${tableHtml(["Category", "Qty/Hrs", "Rate", "Amount"], totalsRows)}
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
  const vehicleLabel = report.vehicleLabel || {};
  const customer = report.customer || {};
  const summary = report.summary || {};
  const calc = report.calculation || {};
  const parts = report.parts || {};

  lines.push("BMB COLLISION REPAIR AI");
  lines.push(`Generated: ${report.generatedAt || new Date().toISOString()}`);
  lines.push(`VIN: ${vehicle.vin || "N/A"}`);
  lines.push(`Vehicle: ${[vehicle.year, vehicle.make, vehicle.model].filter(Boolean).join(" ") || "N/A"}`);
  lines.push(`Trim: ${vehicle.trim || "N/A"}`);
  lines.push(`Paint: ${vehicle.paintCode || "Unknown"} ${vehicle.paintDescription ? `(${vehicle.paintDescription})` : ""}`);
  if (vehicleLabel.paintCode || vehicleLabel.vin) {
    lines.push(`Door-jamb label VIN: ${vehicleLabel.vin || "N/A"} | paint: ${vehicleLabel.paintCode || "Unknown"} ${vehicleLabel.paintDescription ? `(${vehicleLabel.paintDescription})` : ""}`);
  }
  lines.push(`Customer: ${customer.fullName || "N/A"}`);
  lines.push(`Customer address: ${customer.address || "N/A"}`);
  lines.push(`Customer phone: ${customer.phone || "N/A"}`);
  lines.push(`Customer email: ${customer.email || "N/A"}`);
  lines.push(`Source: ${report.source || "unknown"}`);
  lines.push(`Context: ${summary.estimateType || "preliminary"} | impacts: ${(summary.impacts || []).join(", ")} | severity: ${summary.severity || "functional"}`);
  lines.push(`Estimated total: $${Number(calc.grandTotal || 0).toFixed(2)}`);
  lines.push("");

  lines.push("OUTPUT 1 - REPAIR VS REPLACE");
  if (Array.isArray(calc.lineItems) && calc.lineItems.length) {
    for (const row of calc.lineItems) {
      lines.push(`- ${row.component} | ${row.action} | ${row.laborType} | ${Number(row.laborHours || 0).toFixed(2)} hr | $${Number(row.laborTotal || 0).toFixed(2)}`);
      lines.push(`  Notes: ${row.notes}`);
    }
  } else {
    for (const row of report.output1 || []) {
      lines.push(`- ${row.component} | ${row.action} | ${row.laborBucket} | ${row.confidence}`);
      lines.push(`  Notes: ${row.notes}`);
    }
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
  lines.push("ESTIMATE TOTALS");
  lines.push(`- Labor subtotal: $${Number(calc.laborSubtotal || 0).toFixed(2)}`);
  lines.push(`- Paint materials: $${Number(calc.paintMaterialsTotal || 0).toFixed(2)}`);
  lines.push(`- OEM parts: $${Number(calc.partsSubtotal || 0).toFixed(2)}`);
  lines.push(`- Inside storage: $${Number(calc.insideStorageTotal || 0).toFixed(2)}`);
  lines.push(`- Outside storage: $${Number(calc.outsideStorageTotal || 0).toFixed(2)}`);
  lines.push(`- Towing: $${Number(calc.towingTotal || 0).toFixed(2)}`);
  lines.push(`- Grand total: $${Number(calc.grandTotal || 0).toFixed(2)}`);
  lines.push("");

  lines.push("");
  lines.push("OEM PARTS");
  for (const item of parts.items || []) {
    lines.push(`- ${item.component} | PN: ${item.partNumber || "N/A"} | Qty: ${Number(item.quantity || 0).toFixed(2)} | List: $${Number(item.listPrice || 0).toFixed(2)} | Line: $${Number(item.lineTotal || 0).toFixed(2)}`);
  }
  lines.push(`- Parts subtotal: $${Number(parts.subtotal || 0).toFixed(2)}`);
  lines.push("");

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
  const rates = inputs.rates || {};
  const charges = inputs.charges || {};
  const bodyRate = Number(rates.bodyLaborPerHour || 83);
  const laborHours = 2.5;
  const laborSubtotal = laborHours * bodyRate;
  const paintMaterials = Number(rates.paintMaterialsPerPaintHour || 46) * 1.5;
  const partsSubtotal = 0;
  const insideStorageTotal = Number(charges.insideStorageDays || 0) * Number(rates.insideStoragePerDay || 180);
  const outsideStorageTotal = Number(charges.outsideStorageDays || 0) * Number(rates.outsideStoragePerDay || 180);
  const towingTotal = Number(charges.towingMiles || 0) * Number(rates.towingPerMile || 12);
  const grandTotal = laborSubtotal + paintMaterials + partsSubtotal + insideStorageTotal + outsideStorageTotal + towingTotal;

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
    vehicleLabel: inputs.vehicleLabel || {},
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
    rates,
    parts: {
      source: "not-configured",
      items: [],
      subtotal: 0,
      assumptions: ["OEM parts provider not configured in fallback mode."]
    },
    calculation: {
      lineItems: [
        {
          component: output1[0].component,
          action: output1[0].action,
          laborType: "body",
          laborHours,
          ratePerHour: bodyRate,
          laborTotal: laborSubtotal,
          paintHours: 1.5,
          notes: output1[0].notes
        }
      ],
      laborByType: {
        body: { hours: laborHours, rate: bodyRate, total: laborSubtotal },
        structural: { hours: 0, rate: Number(rates.structuralLaborPerHour || 83), total: 0 },
        frame: { hours: 0, rate: Number(rates.frameLaborPerHour || 135), total: 0 },
        mechanical: { hours: 0, rate: Number(rates.mechanicalLaborPerHour || 175), total: 0 },
        electrical: { hours: 0, rate: Number(rates.electricalLaborPerHour || 150), total: 0 },
        paint: { hours: 1.5, rate: Number(rates.bodyLaborPerHour || 83), total: 1.5 * Number(rates.bodyLaborPerHour || 83) }
      },
      rates: {
        insideStoragePerDay: Number(rates.insideStoragePerDay || 180),
        outsideStoragePerDay: Number(rates.outsideStoragePerDay || 180),
        towingPerMile: Number(rates.towingPerMile || 12)
      },
      charges: {
        insideStorageDays: Number(charges.insideStorageDays || 0),
        outsideStorageDays: Number(charges.outsideStorageDays || 0),
        towingMiles: Number(charges.towingMiles || 0)
      },
      laborSubtotal,
      paintMaterialsTotal: paintMaterials,
      partsSubtotal,
      insideStorageTotal,
      outsideStorageTotal,
      towingTotal,
      grandTotal
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
    if (state.vehicleLabelFile) {
      formData.append("vehicleLabel", state.vehicleLabelFile, state.vehicleLabelFile.name);
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
    if (json.report && json.report.vehicleLabel) {
      state.extractedVehicleLabel = json.report.vehicleLabel;
      applyVehicleLabelData(state.extractedVehicleLabel);
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
    const formData = new FormData();
    formData.append("report", JSON.stringify(state.lastReport));
    for (const photoFile of state.photos) {
      formData.append("photos", photoFile, photoFile.name);
    }

    const response = await fetch("/api/report/pdf", {
      method: "POST",
      body: formData
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

refs.vehicleLabelPhoto.addEventListener("change", (event) => {
  state.vehicleLabelFile = (event.target.files && event.target.files[0]) || null;
  updateVehicleLabelPreview();
  refs.vehicleLabelStatus.classList.remove("warn");
  refs.vehicleLabelStatus.textContent = state.vehicleLabelFile ? "Door-jamb label selected. Click Extract Label." : "No door-jamb label processed yet.";
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
refs.extractVehicleLabel.addEventListener("click", extractVehicleLabel);
refs.extractLicense.addEventListener("click", extractLicense);
refs.generate.addEventListener("click", generateEstimate);
refs.downloadPdf.addEventListener("click", downloadPdf);
refs.copyReport.addEventListener("click", copyReport);
refs.talkToggle.addEventListener("click", toggleVoiceRecognition);
refs.vin.addEventListener("input", () => {
  refs.vin.value = sanitizeVin(refs.vin.value);
});

renderDecodedVehicle(null, "");
clearVehicleLabelPreview();
clearLicensePreview();
initVoiceRecognition();
