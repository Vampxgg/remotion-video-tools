const DEMO_FILE = "../azure_model_inventory_2026-06-18_v2.xlsx";

const $ = (id) => document.getElementById(id);

const state = {
  workbookName: "",
  sheets: [],
  activeSheetIndex: 0,
  search: "",
  filterMode: "contains",
  viewMode: "table",
  dirtyCells: new Map(),
};

const ui = {
  fileInput: $("fileInput"),
  dropZone: $("dropZone"),
  loadDemoBtn: $("loadDemoBtn"),
  clearWorkbookBtn: $("clearWorkbookBtn"),
  fileMeta: $("fileMeta"),
  sheetCount: $("sheetCount"),
  rowCount: $("rowCount"),
  colCount: $("colCount"),
  dirtyCount: $("dirtyCount"),
  sheetSummary: $("sheetSummary"),
  downloadBtn: $("downloadBtn"),
  downloadJsonBtn: $("downloadJsonBtn"),
  addRowBtn: $("addRowBtn"),
  addColBtn: $("addColBtn"),
  workbookTitle: $("workbookTitle"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  statusHint: $("statusHint"),
  searchInput: $("searchInput"),
  filterMode: $("filterMode"),
  viewMode: $("viewMode"),
  sheetTabs: $("sheetTabs"),
  sheetTitle: $("sheetTitle"),
  sheetMetaBadge: $("sheetMetaBadge"),
  tableContainer: $("tableContainer"),
  jsonPreview: $("jsonPreview"),
  changeBadge: $("changeBadge"),
  changeList: $("changeList"),
};

init();

function init() {
  bindEvents();
  renderEmptyState();
}

function bindEvents() {
  ui.fileInput.addEventListener("change", async (event) => {
    const [file] = event.target.files || [];
    if (file) {
      await loadFile(file);
      ui.fileInput.value = "";
    }
  });

  ui.dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    ui.dropZone.classList.add("dragover");
  });

  ui.dropZone.addEventListener("dragleave", () => {
    ui.dropZone.classList.remove("dragover");
  });

  ui.dropZone.addEventListener("drop", async (event) => {
    event.preventDefault();
    ui.dropZone.classList.remove("dragover");
    const [file] = event.dataTransfer?.files || [];
    if (file) {
      await loadFile(file);
    }
  });

  ui.loadDemoBtn.addEventListener("click", loadDemoWorkbook);
  ui.clearWorkbookBtn.addEventListener("click", clearWorkbook);
  ui.searchInput.addEventListener("input", () => {
    state.search = ui.searchInput.value.trim().toLowerCase();
    renderActiveSheet();
  });
  ui.filterMode.addEventListener("change", () => {
    state.filterMode = ui.filterMode.value;
    renderActiveSheet();
  });
  ui.viewMode.addEventListener("change", () => {
    state.viewMode = ui.viewMode.value;
    renderActiveSheet();
  });
  ui.downloadBtn.addEventListener("click", exportWorkbook);
  ui.downloadJsonBtn.addEventListener("click", exportJson);
  ui.addRowBtn.addEventListener("click", addRow);
  ui.addColBtn.addEventListener("click", addColumn);
}

function setStatus(title, hint, mode = "idle") {
  ui.statusText.textContent = title;
  ui.statusHint.textContent = hint;
  ui.statusDot.className = `dot ${mode}`;
}

async function loadFile(file) {
  try {
    setStatus("解析中", `正在读取 ${file.name}`, "idle");
    const buffer = await file.arrayBuffer();
    const workbook = XLSX.read(buffer, { type: "array" });
    hydrateWorkbook(workbook, file.name);
    setStatus("已载入", `${file.name} 已就绪`, "live");
  } catch (error) {
    setStatus("导入失败", error.message || "无法解析文件", "error");
  }
}

async function loadDemoWorkbook() {
  try {
    if (window.location.protocol === "file:") {
      throw new Error("当前是 file:// 直开模式，浏览器通常禁止页面直接读取旁边的示例 Excel。请手动上传，或改用本地静态服务打开页面。");
    }
    setStatus("加载示例", "正在读取当前分析表", "idle");
    const response = await fetch(DEMO_FILE);
    if (!response.ok) {
      throw new Error(`示例文件读取失败: ${response.status}`);
    }
    const buffer = await response.arrayBuffer();
    const workbook = XLSX.read(buffer, { type: "array" });
    hydrateWorkbook(workbook, "azure_model_inventory_2026-06-18_v2.xlsx");
    setStatus("已载入", "当前分析表已加载", "live");
  } catch (error) {
    setStatus("示例加载失败", error.message || "请手动上传文件", "error");
  }
}

function hydrateWorkbook(workbook, workbookName) {
  state.workbookName = workbookName;
  state.sheets = workbook.SheetNames.map((sheetName) => {
    const rows = XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], {
      header: 1,
      defval: "",
      raw: false,
    });
    return {
      name: sheetName,
      rows: normalizeRows(rows),
    };
  });
  state.activeSheetIndex = 0;
  state.dirtyCells = new Map();
  ui.searchInput.value = "";
  state.search = "";
  renderWorkbook();
}

function normalizeRows(rows) {
  if (!rows.length) {
    return [[""]];
  }
  const maxCols = rows.reduce((max, row) => Math.max(max, row.length), 0);
  return rows.map((row) => {
    const normalized = [...row];
    while (normalized.length < maxCols) {
      normalized.push("");
    }
    return normalized;
  });
}

function renderWorkbook() {
  renderWorkbookMeta();
  renderSheetTabs();
  renderSheetSummary();
  renderChangeList();
  renderActiveSheet();
}

function renderWorkbookMeta() {
  const totalRows = state.sheets.reduce((sum, sheet) => sum + sheet.rows.length, 0);
  const totalCols = state.sheets.reduce(
    (sum, sheet) => sum + (sheet.rows[0] ? sheet.rows[0].length : 0),
    0,
  );

  ui.workbookTitle.textContent = state.workbookName || "未打开工作簿";
  ui.fileMeta.textContent = state.workbookName
    ? `当前文件：${state.workbookName}`
    : "尚未导入文件";
  ui.sheetCount.textContent = String(state.sheets.length);
  ui.rowCount.textContent = String(totalRows);
  ui.colCount.textContent = String(totalCols);
  ui.dirtyCount.textContent = String(state.dirtyCells.size);
  ui.downloadBtn.disabled = !state.sheets.length;
  ui.downloadJsonBtn.disabled = !state.sheets.length;
}

function renderSheetTabs() {
  ui.sheetTabs.innerHTML = "";
  ui.sheetTabs.classList.toggle("empty", state.sheets.length === 0);

  if (!state.sheets.length) {
    ui.sheetTabs.textContent = "导入后可切换工作表";
    return;
  }

  state.sheets.forEach((sheet, index) => {
    const button = document.createElement("button");
    button.className = `sheet-tab${index === state.activeSheetIndex ? " active" : ""}`;
    button.textContent = sheet.name;
    button.addEventListener("click", () => {
      state.activeSheetIndex = index;
      renderSheetTabs();
      renderActiveSheet();
    });
    ui.sheetTabs.appendChild(button);
  });
}

function renderSheetSummary() {
  ui.sheetSummary.innerHTML = "";
  ui.sheetSummary.classList.toggle("empty", state.sheets.length === 0);

  if (!state.sheets.length) {
    ui.sheetSummary.textContent = "导入文件后会显示每个工作表的规模和用途。";
    return;
  }

  state.sheets.forEach((sheet, index) => {
    const item = document.createElement("div");
    item.className = "sheet-summary-item";
    const rows = sheet.rows.length;
    const cols = sheet.rows[0]?.length || 0;
    const firstHeader = sheet.rows[0]?.slice(0, 3).filter(Boolean).join(" / ") || "无表头";
    item.innerHTML = `
      <strong>${sheet.name}</strong>
      <div>${rows} 行 · ${cols} 列</div>
      <small>${firstHeader}</small>
    `;
    item.addEventListener("click", () => {
      state.activeSheetIndex = index;
      renderSheetTabs();
      renderActiveSheet();
    });
    ui.sheetSummary.appendChild(item);
  });
}

function renderActiveSheet() {
  const sheet = state.sheets[state.activeSheetIndex];
  if (!sheet) {
    renderEmptyState();
    return;
  }

  ui.sheetTitle.textContent = sheet.name;
  ui.sheetMetaBadge.textContent = `${sheet.rows.length} 行 · ${sheet.rows[0]?.length || 0} 列`;

  if (state.viewMode === "json") {
    ui.tableContainer.classList.add("hidden");
    ui.jsonPreview.classList.remove("hidden");
    ui.jsonPreview.textContent = JSON.stringify(rowsToObjects(sheet.rows), null, 2);
    return;
  }

  ui.tableContainer.classList.remove("hidden");
  ui.jsonPreview.classList.add("hidden");
  renderTable(sheet);
}

function renderTable(sheet) {
  ui.tableContainer.classList.remove("empty");
  ui.tableContainer.innerHTML = "";

  const table = document.createElement("table");
  table.className = "data-grid";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");

  const rowIndexHead = document.createElement("th");
  rowIndexHead.className = "row-index-head";
  rowIndexHead.textContent = "#";
  headRow.appendChild(rowIndexHead);

  const headers = sheet.rows[0] || [];
  headers.forEach((header, colIndex) => {
    const th = document.createElement("th");
    th.textContent = header || `列 ${colIndex + 1}`;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  sheet.rows.forEach((row, rowIndex) => {
    const tr = document.createElement("tr");
    const rowText = row.map((cell) => String(cell ?? "")).join(" ").toLowerCase();
    const rowHasDirty = row.some((_, colIndex) =>
      state.dirtyCells.has(buildDirtyKey(sheet.name, rowIndex, colIndex)),
    );

    const shouldHide = shouldHideRow(rowIndex, rowText, rowHasDirty);
    if (shouldHide) {
      tr.classList.add("hidden-row");
    }

    const indexCell = document.createElement("td");
    indexCell.className = "row-index";
    indexCell.textContent = String(rowIndex + 1);
    tr.appendChild(indexCell);

    row.forEach((value, colIndex) => {
      const td = document.createElement("td");
      td.className = "editable";
      const dirtyKey = buildDirtyKey(sheet.name, rowIndex, colIndex);
      const text = String(value ?? "");
      td.textContent = text;
      td.title = "点击编辑";

      if (state.search && text.toLowerCase().includes(state.search)) {
        td.classList.add("cell-search-hit");
      }
      if (state.dirtyCells.has(dirtyKey)) {
        td.classList.add("cell-dirty");
      }

      td.addEventListener("click", () => startCellEdit(td, sheet, rowIndex, colIndex));
      td.addEventListener("dblclick", () => startCellEdit(td, sheet, rowIndex, colIndex));
      tr.appendChild(td);
    });

    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  ui.tableContainer.appendChild(table);
}

function shouldHideRow(rowIndex, rowText, rowHasDirty) {
  if (state.filterMode === "all") {
    return false;
  }
  if (state.filterMode === "changed") {
    return !rowHasDirty;
  }
  if (!state.search) {
    return false;
  }
  if (rowIndex === 0) {
    return false;
  }
  return !rowText.includes(state.search);
}

function startCellEdit(cell, sheet, rowIndex, colIndex) {
  if (cell.classList.contains("editing")) {
    return;
  }

  const oldValue = String(sheet.rows[rowIndex][colIndex] ?? "");
  cell.classList.add("editing");
  cell.innerHTML = "";

  const textarea = document.createElement("textarea");
  textarea.className = "cell-editor";
  textarea.value = oldValue;
  cell.appendChild(textarea);
  textarea.focus();
  textarea.select();

  const commit = () => {
    const newValue = textarea.value;
    cell.classList.remove("editing");
    updateCell(sheet, rowIndex, colIndex, newValue, oldValue);
  };

  const cancel = () => {
    cell.classList.remove("editing");
    cell.textContent = oldValue;
  };

  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    } else if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      commit();
    }
  });

  textarea.addEventListener("blur", commit, { once: true });
}

function updateCell(sheet, rowIndex, colIndex, newValue, oldValue) {
  sheet.rows[rowIndex][colIndex] = newValue;
  const key = buildDirtyKey(sheet.name, rowIndex, colIndex);
  if (newValue !== oldValue) {
    state.dirtyCells.set(key, {
      sheetName: sheet.name,
      rowIndex,
      colIndex,
      oldValue,
      newValue,
      header: sheet.rows[0]?.[colIndex] || `列 ${colIndex + 1}`,
    });
  } else {
    state.dirtyCells.delete(key);
  }
  renderWorkbookMeta();
  renderChangeList();
  renderActiveSheet();
}

function buildDirtyKey(sheetName, rowIndex, colIndex) {
  return `${sheetName}::${rowIndex}::${colIndex}`;
}

function renderChangeList() {
  const changes = [...state.dirtyCells.values()];
  ui.changeBadge.textContent = `${changes.length} 项`;

  if (!changes.length) {
    ui.changeList.className = "change-list empty";
    ui.changeList.textContent = "尚未有修改。双击单元格即可编辑。";
    return;
  }

  ui.changeList.className = "change-list";
  ui.changeList.innerHTML = "";

  changes
    .sort((a, b) => {
      const sheetCompare = a.sheetName.localeCompare(b.sheetName, "zh-CN");
      if (sheetCompare !== 0) {
        return sheetCompare;
      }
      if (a.rowIndex !== b.rowIndex) {
        return a.rowIndex - b.rowIndex;
      }
      return a.colIndex - b.colIndex;
    })
    .forEach((change) => {
      const item = document.createElement("div");
      item.className = "change-item";
      item.innerHTML = `
        <strong>${change.sheetName} · 第 ${change.rowIndex + 1} 行 · ${change.header}</strong>
        <div>原值：${shorten(change.oldValue)}</div>
        <div>新值：${shorten(change.newValue)}</div>
      `;
      ui.changeList.appendChild(item);
    });
}

function shorten(value) {
  const text = String(value ?? "");
  return text.length > 80 ? `${text.slice(0, 80)}...` : text || "(空)";
}

function addRow() {
  const sheet = state.sheets[state.activeSheetIndex];
  if (!sheet) {
    return;
  }
  const colCount = sheet.rows[0]?.length || 1;
  sheet.rows.push(Array(colCount).fill(""));
  renderWorkbook();
}

function addColumn() {
  const sheet = state.sheets[state.activeSheetIndex];
  if (!sheet) {
    return;
  }
  sheet.rows.forEach((row, index) => {
    row.push(index === 0 ? `新增列 ${row.length + 1}` : "");
  });
  renderWorkbook();
}

function exportWorkbook() {
  if (!state.sheets.length) {
    return;
  }

  const workbook = XLSX.utils.book_new();
  state.sheets.forEach((sheet) => {
    const worksheet = XLSX.utils.aoa_to_sheet(sheet.rows);
    XLSX.utils.book_append_sheet(workbook, worksheet, sheet.name);
  });

  const filename = buildExportName(".xlsx");
  XLSX.writeFile(workbook, filename);
  setStatus("已导出", `已生成 ${filename}`, "live");
}

function exportJson() {
  if (!state.sheets.length) {
    return;
  }
  const payload = {
    workbookName: state.workbookName,
    sheets: state.sheets.map((sheet) => ({
      name: sheet.name,
      rows: sheet.rows,
      objects: rowsToObjects(sheet.rows),
    })),
  };

  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  downloadBlob(blob, buildExportName(".json"));
  setStatus("已导出", "JSON 导出成功", "live");
}

function rowsToObjects(rows) {
  if (!rows.length) {
    return [];
  }
  const headers = rows[0].map((header, index) => header || `列_${index + 1}`);
  return rows.slice(1).map((row) => {
    const item = {};
    headers.forEach((header, index) => {
      item[header] = row[index] ?? "";
    });
    return item;
  });
}

function buildExportName(ext) {
  const base = (state.workbookName || "model-inventory")
    .replace(/\.[^.]+$/, "")
    .replace(/[^\w\u4e00-\u9fa5-]+/g, "_");
  return `${base}_edited${ext}`;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function clearWorkbook() {
  state.workbookName = "";
  state.sheets = [];
  state.activeSheetIndex = 0;
  state.search = "";
  state.dirtyCells = new Map();
  ui.searchInput.value = "";
  renderEmptyState();
  setStatus("已清空", "工作区已重置", "idle");
}

function renderEmptyState() {
  ui.workbookTitle.textContent = "未打开工作簿";
  ui.fileMeta.textContent = "尚未导入文件";
  ui.sheetCount.textContent = "0";
  ui.rowCount.textContent = "0";
  ui.colCount.textContent = "0";
  ui.dirtyCount.textContent = "0";
  ui.sheetSummary.className = "sheet-summary empty";
  ui.sheetSummary.textContent = "导入文件后会显示每个工作表的规模和用途。";
  ui.sheetTabs.className = "sheet-tabs empty";
  ui.sheetTabs.textContent = "导入后可切换工作表";
  ui.sheetTitle.textContent = "工作表预览";
  ui.sheetMetaBadge.textContent = "0 行 · 0 列";
  ui.tableContainer.className = "table-container empty";
  ui.tableContainer.innerHTML = "<p>导入 Excel 后，在这里实时预览和编辑表格。</p>";
  ui.jsonPreview.classList.add("hidden");
  ui.jsonPreview.textContent = "";
  ui.changeList.className = "change-list empty";
  ui.changeList.textContent = "尚未有修改。双击单元格即可编辑。";
  ui.changeBadge.textContent = "0 项";
  ui.downloadBtn.disabled = true;
  ui.downloadJsonBtn.disabled = true;
}
