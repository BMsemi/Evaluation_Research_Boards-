const state = {
  selectedRun: "",
  activeKey: "",
  commandsEnabled: false,
  manualRun: false,
};

const els = {
  runSelect: document.getElementById("runSelect"),
  followBtn: document.getElementById("followBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  subtitle: document.getElementById("subtitle"),
  apiSignal: document.getElementById("apiSignal"),
  tickerText: document.getElementById("tickerText"),
  lastCell: document.getElementById("lastCell"),
  lastCurrent: document.getElementById("lastCurrent"),
  heatmap: document.getElementById("heatmap"),
  scale: document.getElementById("scale"),
  activeCell: document.getElementById("activeCell"),
  opDot: document.getElementById("opDot"),
  opCompact: document.getElementById("opCompact"),
  packetCompact: document.getElementById("packetCompact"),
  chart: document.getElementById("chart"),
  commandForm: document.getElementById("commandForm"),
  operationInput: document.getElementById("operationInput"),
  rowInput: document.getElementById("rowInput"),
  colInput: document.getElementById("colInput"),
  zynqPasswordInput: document.getElementById("zynqPasswordInput"),
  dryRunInput: document.getElementById("dryRunInput"),
  commandBtn: document.getElementById("commandBtn"),
  killBtn: document.getElementById("killBtn"),
  commandNote: document.getElementById("commandNote"),
};

function formatCurrent(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}` : "--";
}

function formatCell(cell) {
  return cell ? `r${String(cell.row).padStart(2, "0")} c${String(cell.col).padStart(2, "0")}` : "--";
}

function cellKey(cell) {
  return cell ? `${cell.row}_${cell.col}` : "";
}

function colorFor(value, min, max) {
  if (!Number.isFinite(value)) return "#2a2d34";
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return "#4cc9a6";
  const t = Math.max(0, Math.min(1, (value - min) / (max - min)));
  const hue = 205 - t * 170;
  const light = 38 + t * 20;
  return `hsl(${hue}, 78%, ${light}%)`;
}

function ensureGrid() {
  if (els.heatmap.children.length === 1024) return;
  els.heatmap.innerHTML = "";
  for (let row = 0; row < 32; row += 1) {
    for (let col = 0; col < 32; col += 1) {
      const button = document.createElement("button");
      button.className = "cell";
      button.type = "button";
      button.dataset.key = `${row}_${col}`;
      button.title = `r${row} c${col}: no reading`;
      button.addEventListener("click", () => {
        els.rowInput.value = row;
        els.colInput.value = col;
      });
      els.heatmap.appendChild(button);
    }
  }
}

function renderRuns(runs, currentRunId) {
  const previous = state.manualRun ? state.selectedRun || els.runSelect.value : currentRunId;
  els.runSelect.innerHTML = "";
  for (const run of runs) {
    const option = document.createElement("option");
    option.value = run.id;
    option.textContent = run.id;
    els.runSelect.appendChild(option);
  }
  if (runs.some((run) => run.id === previous)) {
    els.runSelect.value = previous;
  }
  state.selectedRun = els.runSelect.value;
  renderFollowMode();
}

function renderFollowMode() {
  els.followBtn.textContent = state.manualRun ? "PINNED" : "FOLLOWING";
  els.followBtn.title = state.manualRun ? "Pinned to selected run. Click to follow latest." : "Following latest active run. Click to pin current run.";
  els.followBtn.classList.toggle("active", !state.manualRun);
  els.followBtn.classList.toggle("pinned", state.manualRun);
}

function renderMetrics(summary) {
  const last = summary?.last;
  const activeCell = summary?.lastCell;
  const lastReadCell = summary?.lastReadCell;
  els.subtitle.textContent = summary ? summary.run.id : "No runs";
  els.lastCell.textContent = formatCell(lastReadCell);
  els.lastCurrent.textContent = formatCurrent(summary?.lastCurrent_uA);

  const op = last?.operation || "--";
  els.activeCell.textContent = formatCell(activeCell);
  els.opCompact.textContent = op;
  els.packetCompact.textContent = last?.packet || "--";
  els.opDot.className = `dot ${op === "read" ? "read" : op === "--" ? "" : "program"}`;
  els.apiSignal.className = `signal ${summary?.activeError ? "bad" : last ? (last.ok ? "good" : "bad") : ""}`;
  renderTicker(summary);
}

function renderTicker(summary) {
  const history = summary?.history || [];
  const logEvents = summary?.logEvents || [];
  const rows = [...history, ...logEvents]
    .sort((a, b) => eventOrder(a) - eventOrder(b))
    .slice(-2)
    .reverse()
    .map(formatApiEvent);
  const first = rows[0] || "Waiting for the first API result.";
  const second = rows[1] || "--";
  els.tickerText.innerHTML = "";
  for (const [index, text] of [first, second].entries()) {
    const line = document.createElement("div");
    line.className = `ticker-line ${index === 1 ? "muted-line" : ""}`;
    line.textContent = text;
    els.tickerText.appendChild(line);
  }
}

function eventOrder(row) {
  if (Number.isFinite(row.eventOrder)) return row.eventOrder;
  if (Number.isFinite(row.updated)) return row.updated * 1000;
  if (Number.isFinite(row.index)) return row.index;
  const match = String(row.index || "").match(/capture_(\d+)/);
  return match ? Number(match[1]) : 0;
}

function formatApiEvent(row) {
  if (row.source === "log") {
    return `ERROR: ${row.message}`;
  }
  const cell = formatCell(row.cellAddress);
  const packet = row.packet ? `packet ${row.packet}` : "packet unknown";
  const rails = Number.isFinite(row.vcc_set_V) && Number.isFinite(row.vcc_wl_set_V)
    ? `rails ${row.vcc_set_V} V / ${row.vcc_wl_set_V} V`
    : "rails unknown";
  const status = row.ok ? "decoded OK" : "needs check";
  if (isRead(row)) {
    return `Read ${cell}: ${formatCurrent(row.current_uA)} uA, ${packet}, ${status}`;
  }
  return `${String(row.operation || "Program").toUpperCase()} pulse at ${cell}: ${rails}, ${packet}, ${status}`;
}

function renderHeatmap(summary) {
  ensureGrid();
  const min = summary?.scale?.min_uA;
  const max = summary?.scale?.max_uA;
  const activeKey = cellKey(summary?.lastCell);
  const latest = new Map();
  for (const item of summary?.cells || []) latest.set(cellKey(item.cellAddress), item);
  for (const node of els.heatmap.children) {
    const item = latest.get(node.dataset.key);
    const value = item?.current_uA;
    node.style.background = colorFor(value, min, max);
    node.classList.toggle("active", node.dataset.key === activeKey);
    node.title = item ? `${formatCell(item.cellAddress)} ${formatCurrent(value)} uA ${item.operation}` : `${node.dataset.key}: no read`;
  }
  els.scale.textContent = Number.isFinite(min) && Number.isFinite(max) ? `${min.toFixed(1)}...${max.toFixed(1)} uA` : "--";
}

function renderChart(summary) {
  const canvas = els.chart;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.fillStyle = "#111318";
  ctx.fillRect(0, 0, rect.width, rect.height);

  const pulses = buildPulseSeries(summary?.history || []);
  if (!pulses.length) return;

  const padLeft = 60;
  const padRight = 18;
  const padTop = 24;
  const gap = 14;
  const topH = Math.max(120, rect.height * 0.56);
  const bottomY = padTop + topH + gap;
  const bottomH = rect.height - bottomY - 28;
  const plotW = rect.width - padLeft - padRight;
  const xFor = (index) => padLeft + (plotW * index) / Math.max(1, pulses.length - 1);

  const currentValues = pulses.map((item) => item.current).filter(Number.isFinite);
  const thresholdValues = Object.values(summary?.thresholds_uA || {}).map(Number).filter(Number.isFinite);
  const rawMin = Math.min(...currentValues, 0);
  const rawMax = 400;
  const currentMin = Math.floor(rawMin / 25) * 25;
  const currentMax = Math.ceil(rawMax / 25) * 25;
  const currentSpan = currentMax === currentMin ? 1 : currentMax - currentMin;
  const yCurrent = (value) => {
    const clamped = Math.max(currentMin, Math.min(currentMax, value));
    return padTop + topH - ((clamped - currentMin) / currentSpan) * topH;
  };

  const voltageMax = Math.max(2, ...pulses.map((item) => Math.abs(item.voltage || 0)));
  const yZero = bottomY + bottomH / 2;
  const yVoltage = (value) => yZero - (value / voltageMax) * (bottomH / 2 - 6);

  drawAxes(ctx, padLeft, padTop, plotW, topH, bottomY, bottomH, rect.width, rect.height);
  drawThresholds(ctx, padLeft, plotW, yCurrent, currentMin, currentMax, summary?.thresholds_uA || {});
  drawTransition(ctx, pulses, xFor, padTop, topH, bottomY, bottomH);
  drawCurrentTrace(ctx, pulses, xFor, yCurrent);
  drawVoltageBars(ctx, pulses, xFor, yZero, yVoltage);
  drawLabels(ctx, pulses, currentMin, currentMax, voltageMax, padLeft, padTop, topH, bottomY, rect.height);
}

function isRead(row) {
  return row?.operation === "read" || String(row?.stage || "").startsWith("read");
}

function buildPulseSeries(history) {
  const pulses = [];
  let pending = null;
  for (const row of history) {
    if (!row?.cellAddress) continue;
    if (!isRead(row)) {
      pending = {
        cell: row.cellAddress,
        op: row.operation,
        packet: row.packet,
        voltage: signedVoltage(row),
        current: null,
      };
      pulses.push(pending);
      continue;
    }
    if (pending && pending.current === null) {
      pending.current = row.current_uA;
      pending.readStage = row.stage;
    } else {
      pulses.push({
        cell: row.cellAddress,
        op: "read",
        packet: row.packet,
        voltage: 0,
        current: row.current_uA,
      });
    }
  }
  return pulses.slice(-80);
}

function signedVoltage(row) {
  const value = Number.isFinite(row.vcc_wl_set_V) ? row.vcc_wl_set_V : row.vcc_set_V;
  if (!Number.isFinite(value)) return 0;
  return row.operation === "set" ? -value : value;
}

function drawAxes(ctx, left, top, width, topH, bottomY, bottomH, totalW, totalH) {
  ctx.strokeStyle = "#d9dee6";
  ctx.lineWidth = 1.2;
  ctx.strokeRect(left, top, width, topH);
  ctx.strokeRect(left, bottomY, width, bottomH);
  ctx.strokeStyle = "#8b949e";
  ctx.beginPath();
  ctx.moveTo(left, bottomY + bottomH / 2);
  ctx.lineTo(left + width, bottomY + bottomH / 2);
  ctx.stroke();
  ctx.fillStyle = "#d9dee6";
  ctx.font = "12px system-ui";
  ctx.save();
  ctx.translate(14, top + topH / 2 + 34);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Current (uA)", 0, 0);
  ctx.restore();
  ctx.save();
  ctx.translate(14, bottomY + bottomH / 2 + 28);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Voltage (V)", 0, 0);
  ctx.restore();
  ctx.fillText("Pulse", totalW / 2 - 12, totalH - 8);
}

function drawThresholds(ctx, left, width, yCurrent, currentMin, currentMax, thresholds) {
  const visible = [
    ["SET", Number(thresholds.set), "#ff3b4f"],
    ["RESET", Number(thresholds.reset), "#6ab6df"],
  ].filter(([, threshold]) => Number.isFinite(threshold));
  const usedY = [];
  visible.forEach(([label, threshold, color]) => {
    if (threshold < currentMin || threshold > currentMax) return;
    const y = yCurrent(threshold);
    let labelY = y - 7;
    for (const prevY of usedY) {
      if (Math.abs(labelY - prevY) < 16) labelY = prevY + 16;
    }
    usedY.push(labelY);
    ctx.strokeStyle = color;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "12px system-ui";
    const text = `${label} ${threshold} uA`;
    const textW = ctx.measureText(text).width;
    const textX = left + width - textW - 8;
    ctx.fillStyle = "rgba(17, 19, 24, 0.88)";
    ctx.fillRect(textX - 4, labelY - 11, textW + 8, 15);
    ctx.fillStyle = color;
    ctx.fillText(text, textX, labelY);
  });
}

function drawTransition(ctx, pulses, xFor, top, topH, bottomY, bottomH) {
  const index = pulses.findIndex((item, i) => item.op === "reset" && pulses.slice(0, i).some((prev) => prev.op === "set"));
  if (index < 0) return;
  const x = xFor(index);
  ctx.strokeStyle = "#d9dee6";
  ctx.setLineDash([6, 5]);
  ctx.beginPath();
  ctx.moveTo(x, top);
  ctx.lineTo(x, bottomY + bottomH);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawCurrentTrace(ctx, pulses, xFor, yCurrent) {
  const points = pulses
    .map((item, index) => ({ ...item, index }))
    .filter((item) => Number.isFinite(item.current));
  ctx.strokeStyle = "#4fb3ad";
  ctx.lineWidth = 1.7;
  ctx.beginPath();
  points.forEach((item, i) => {
    const x = xFor(item.index);
    const y = yCurrent(item.current);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  for (const item of points) {
    const x = xFor(item.index);
    const y = yCurrent(item.current);
    ctx.fillStyle = "#111318";
    ctx.strokeStyle = "#6bd2cc";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}

function drawVoltageBars(ctx, pulses, xFor, yZero, yVoltage) {
  const barW = Math.max(2, Math.min(8, 460 / Math.max(20, pulses.length)));
  pulses.forEach((item, index) => {
    if (!Number.isFinite(item.voltage) || item.voltage === 0) return;
    const x = xFor(index);
    const y = yVoltage(item.voltage);
    ctx.strokeStyle = item.op === "set" ? "#ff3b4f" : "#6ab6df";
    ctx.lineWidth = barW;
    ctx.beginPath();
    ctx.moveTo(x, yZero);
    ctx.lineTo(x, y);
    ctx.stroke();
  });
}

function drawLabels(ctx, pulses, currentMin, currentMax, voltageMax, left, top, topH, bottomY, totalH) {
  const cell = pulses.find((item) => item.cell)?.cell;
  ctx.fillStyle = "#f3f5f7";
  ctx.font = "600 13px system-ui";
  ctx.fillText(cell ? `Cell (${cell.row},${cell.col})` : "Cell", left, 16);
  ctx.font = "12px system-ui";
  ctx.fillStyle = "#9aa4af";
  ctx.fillText(`${currentMax} uA`, 4, top + 5);
  ctx.fillText(`${currentMin} uA`, 4, top + topH - 4);
  ctx.fillText(`+${voltageMax.toFixed(1)} V`, 4, bottomY + 10);
  ctx.fillText(`-${voltageMax.toFixed(1)} V`, 4, totalH - 30);
  ctx.fillStyle = "#ff3b4f";
  ctx.fillRect(left + 250, totalH - 42, 28, 5);
  ctx.fillText("SET", left + 286, totalH - 36);
  ctx.fillStyle = "#6ab6df";
  ctx.fillRect(left + 340, totalH - 42, 28, 5);
  ctx.fillText("RESET", left + 376, totalH - 36);
}

async function refresh() {
  const query = state.manualRun && state.selectedRun ? `?run=${encodeURIComponent(state.selectedRun)}` : "";
  const response = await fetch(`/api/state${query}`, { cache: "no-store" });
  const data = await response.json();
  renderRuns(data.runs || [], data.state?.run?.id || "");
  state.commandsEnabled = Boolean(data.commandsEnabled);
  renderCommandState(data.runningCommands || []);
  renderMetrics(data.state);
  renderHeatmap(data.state);
  renderChart(data.state);
}

function renderCommandState(commands) {
  const running = commands.find((command) => command.running);
  state.runningCommandId = running?.id || "";
  els.commandBtn.disabled = !state.commandsEnabled || Boolean(running);
  els.killBtn.disabled = !running;
  if (!state.commandsEnabled) {
    els.commandNote.textContent = "--allow-commands";
  } else if (running) {
    els.commandNote.textContent = `Processing ${running.operation} r${running.row} c${running.col}`;
  } else {
    els.commandNote.textContent = "Ready";
  }
}

els.runSelect.addEventListener("change", () => {
  state.manualRun = true;
  state.selectedRun = els.runSelect.value;
  refresh();
});
els.followBtn.addEventListener("click", () => {
  state.manualRun = !state.manualRun;
  if (!state.manualRun) state.selectedRun = "";
  renderFollowMode();
  refresh();
});
els.refreshBtn.addEventListener("click", refresh);
els.killBtn.addEventListener("click", async () => {
  if (!state.runningCommandId) return;
  const ok = window.confirm("Kill the GUI-started command that is processing now?");
  if (!ok) return;
  const response = await fetch("/api/command/kill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: state.runningCommandId }),
  });
  const data = await response.json();
  els.commandNote.textContent = data.error || data.message || "Kill signal sent";
  setTimeout(refresh, 500);
});
els.commandForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    operation: els.operationInput.value,
    row: Number(els.rowInput.value),
    col: Number(els.colInput.value),
    zynqPassword: els.zynqPasswordInput.value,
    dryRun: els.dryRunInput.checked,
  };
  if (!payload.dryRun) {
    const ok = window.confirm(
      `Send ${payload.operation.toUpperCase()} to hardware for row ${payload.row}, col ${payload.col}?\n\n` +
      "This will run the API against the connected bench."
    );
    if (!ok) {
      els.commandNote.textContent = "Cancelled";
      return;
    }
    payload.confirmHardware = true;
  }
  const response = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  els.commandNote.textContent = data.error || `Started ${payload.operation} in ${data.runDir}`;
  if (!data.error) state.runningCommandId = data.id;
  setTimeout(refresh, 900);
});

ensureGrid();
refresh();
setInterval(refresh, 1500);
