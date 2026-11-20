"use strict";

const el = id => document.getElementById(id);
const tr = new Intl.NumberFormat("tr-TR", {maximumFractionDigits: 2});
const prices = new Intl.NumberFormat("tr-TR", {minimumFractionDigits: 2, maximumFractionDigits: 3});
const phases = {idle: "Bekliyor", preparing: "Hazırlanıyor", ready: "Hazır", running: "Oynatılıyor",
    paused: "Duraklatıldı", finished: "Tamamlandı", error: "Replay durdu"};
const marketPhases = {P_SUREKLI_ISLEM: "Sürekli işlem", P_GUNSONU: "Gün sonu", P_MARJ_YAYIN: "Marj yayını",
    P_ARA: "Ara", P_ACILIS_EMIR_TPL: "Açılış emir toplama", P_ACS_EMR_TP_EIY: "Açılış sonu",
    P_ESLESTIRME: "Eşleştirme", P_KAPANIS_EMIR_TPL: "Kapanış emir toplama"};
const sideLabel = {B: "Alış", S: "Satış"};
const outgoing = {O: "Yeni emir", U: "Değiştir", X: "İptal et"};
const incoming = {A: "Kabul", J: "Ret", U: "Değişti", C: "İptal", E: "Gerçekleşti"};
let state = {}, actionError = "", initialized = false, requestPending = false, pollPending = false;
let actionRevision = 0;
let chartSeries = "mid_price";
let captureChanged = false, settingsChanged = false, speedChanged = false;
let strategyCatalog = null, strategyListPending = false, strategyListError = "", strategyError = "";
let parameterKey, parameterFields = new Map();
let catalogKey, resultsKey, resultsSearch, chipsKey, clusterKey, previousFocus;
let availableSymbols = [], selectedSymbols = new Set(), instrumentRows = new Map();

function format(value, formatter = tr) {
    return typeof value === "number" && Number.isFinite(value) ? formatter.format(value) : "—";
}

function lira(value) {
    return typeof value === "number" && Number.isFinite(value) ? `${format(value, prices)} ₺` : "—";
}

function value(id, text) {
    const node = el(id);
    if (node && node.textContent !== text) node.textContent = text;
}

function balance(id, amount) {
    value(id, lira(amount));
    el(id).classList.toggle("positive", amount > 0);
    el(id).classList.toggle("negative", amount < 0);
}

function makeInfo(text) {
    const button = document.createElement("button");
    button.className = "info-button";
    button.type = "button";
    button.textContent = "i";
    button.dataset.tip = text;
    button.setAttribute("aria-label", `Bilgi: ${text}`);
    button.setAttribute("aria-expanded", "false");
    return button;
}

for (const placeholder of document.querySelectorAll("[data-help]")) {
    placeholder.append(makeInfo(placeholder.dataset.help));
}

let tipButton = null, pinned = false;
const tooltip = el("info-tooltip");

function closeTip() {
    if (tipButton) tipButton.setAttribute("aria-expanded", "false");
    tipButton = null;
    pinned = false;
    tooltip.hidden = true;
}

function openTip(button, pin = false) {
    closeTip();
    tipButton = button;
    pinned = pin;
    tooltip.textContent = button.dataset.tip;
    tooltip.hidden = false;
    const rect = button.getBoundingClientRect();
    const width = tooltip.offsetWidth, height = tooltip.offsetHeight;
    tooltip.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - width - 8))}px`;
    tooltip.style.top = `${rect.bottom + height + 10 > innerHeight ? rect.top - height - 7 : rect.bottom + 7}px`;
    button.setAttribute("aria-expanded", "true");
}

document.addEventListener("click", event => {
    const button = event.target.closest(".info-button");
    if (!button || (button === tipButton && pinned)) closeTip();
    else openTip(button, true);
});
document.addEventListener("focusin", event => {
    if (event.target.matches(".info-button") && !pinned) openTip(event.target);
});
document.addEventListener("focusout", event => {
    if (event.target === tipButton && !pinned) closeTip();
});
document.addEventListener("keydown", event => { if (event.key === "Escape") closeTip(); });
window.addEventListener("scroll", closeTip, {passive: true});

function cell(row, text, className = "") {
    const td = row.insertCell();
    td.textContent = text;
    if (className) td.className = className;
    return td;
}

function emptyTable(body, count, message) {
    const node = cell(body.insertRow(), message, "empty-row");
    node.colSpan = count;
}

let previousBook, previousJournal;

function bookRows() {
    const historical = el("book-source").value === "historical";
    const book = (historical ? state.reference_book : state.book) || {};
    const depth = Number(el("book-depth").value);
    const asks = (book.asks || []).slice(0, depth), bids = (book.bids || []).slice(0, depth);
    const spread = asks.length && bids.length ? asks[0].price - bids[0].price : null;
    value("spread", lira(spread));
    const signature = JSON.stringify([asks, bids]);
    if (signature === previousBook) return;
    previousBook = signature;
    const topQuantity = Math.max(1, ...asks.map(x => x.quantity), ...bids.map(x => x.quantity));
    for (const [id, rows, rowClass, label] of [["bid-rows", bids, "bid-row", "alış"], ["ask-rows", asks, "ask-row", "satış"]]) {
        const body = el(id);
        body.replaceChildren();
        for (const level of rows) {
            const row = body.insertRow();
            row.className = rowClass;
            row.style.setProperty("--depth", `${level.quantity * 100 / topQuantity}%`);
            cell(row, format(level.price, prices), "price-cell");
            cell(row, format(level.quantity));
            cell(row, format(level.orders));
        }
        if (!rows.length) emptyTable(body, 3, `Görünür ${label} emri yok.`);
    }
}

function matchingSummary(audit = {}) {
    value("matching-observed", format(audit.observed_quantity));
    value("matching-strategy", format(audit.strategy_quantity));
    value("matching-historical", format(audit.historical_quantity));
    value("matching-unallocated", format(audit.unallocated_quantity));
    value("matching-reconciled", format(audit.reconcile_events));
    el("book-audit-alert").hidden = !audit.suspended;
}

function journalRows(entries = []) {
    const latest = entries.slice(-80).reverse();
    const signature = JSON.stringify(latest);
    if (signature === previousJournal || (tipButton && pinned && el("journal-rows").contains(tipButton))) return;
    previousJournal = signature;
    const body = el("journal-rows");
    body.replaceChildren();
    for (const event of latest) {
        const row = body.insertRow(), send = event.direction === "send";
        cell(row, format(event.sequence));
        cell(row, event.time || "—");
        cell(row, sideLabel[event.side] || "—", event.side === "B" ? "positive" : event.side === "S" ? "negative" : "");
        const label = (send ? outgoing : incoming)[event.type] || event.type;
        const message = cell(row, `${event.type} · ${label}`);
        if (event.frame_hex) message.append(" ", makeInfo(`Yerel OUCH mesajı. Token ${event.token || "—"}. İstek sırası ${event.request_sequence ?? "—"}. Yanıt sırası ${event.ouch_sequence ?? "—"}. İkili çerçeve ${event.frame_hex}`));
        cell(row, format(event.price, prices));
        cell(row, format(event.quantity));
        cell(row, event.reason_text || (send ? "Gönderildi" : "Yanıt"), event.type === "J" ? "negative" : "");
    }
    if (!latest.length) emptyTable(body, 7, "Henüz OUCH mesajı oluşmadı.");
}

function chart() {
    const points = (state.history || []).filter(item => typeof item[chartSeries] === "number");
    const latest = state.strategy?.[chartSeries], marketTime = state.progress?.market_time;
    if (marketTime && typeof latest === "number" && points.at(-1)?.time !== marketTime) {
        points.push({time: marketTime, [chartSeries]: latest});
    }
    const canvas = el("chart"), area = el("chart-area");
    const width = area.clientWidth, height = area.clientHeight;
    if (!width || !height) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    context.scale(ratio, ratio);
    el("chart-empty").hidden = points.length > 0;
    value("chart-value", lira(state.strategy?.[chartSeries]));
    const measure = chartSeries === "mid_price" ? "Orta fiyat" : "Kâr / zarar";
    canvas.setAttribute("aria-label", state.focus_symbol ? `${state.focus_symbol} ${measure.toLocaleLowerCase("tr-TR")} grafiği` : `${measure} grafiği`);

    const left = 53, right = width - 12, top = 12, bottom = height - 25;
    const numbers = points.map(point => point[chartSeries]);
    let low = numbers.length ? Math.min(...numbers) : 0;
    let high = numbers.length ? Math.max(...numbers) : 1;
    const pad = high === low ? Math.max(0.05, Math.abs(low) * 0.001) : (high - low) * 0.08;
    low -= pad;
    high += pad;
    const times = points.map(point => point.time.split(":").reduce((total, part) => total * 60 + Number(part), 0));
    const duration = times.at(-1) - times[0];
    const x = index => left + (right - left) * (duration > 0 ? (times[index] - times[0]) / duration : 0);
    const y = number => bottom - (bottom - top) * (number - low) / (high - low);
    context.font = "11px system-ui";
    context.strokeStyle = "#28394a";
    context.fillStyle = "#9baec1";
    for (let i = 0; i <= 4; i += 1) {
        const level = low + (high - low) * i / 4;
        const line = y(level);
        context.beginPath();
        context.moveTo(left, line);
        context.lineTo(right, line);
        context.stroke();
        context.textAlign = "right";
        if (points.length) context.fillText(format(level), left - 7, line + 4);
    }
    if (!points.length) return;
    context.beginPath();
    for (const [i, point] of points.entries()) {
        const py = y(point[chartSeries]);
        if (i === 0) context.moveTo(x(i), py);
        else context.lineTo(x(i), py);
    }
    context.strokeStyle = "#5ee6c2";
    context.lineWidth = 2;
    context.stroke();
    if (points.length === 1) {
        context.fillStyle = "#5ee6c2";
        context.beginPath();
        context.arc(left, y(numbers[0]), 3, 0, Math.PI * 2);
        context.fill();
    }
    context.fillStyle = "#9baec1";
    const last = points.length - 1;
    for (const index of new Set([0, Math.floor(last / 2), last])) {
        context.textAlign = index === 0 ? "left" : index === last ? "right" : "center";
        context.fillText((points[index].time || "").slice(0, 8), x(index), height - 5);
    }
}

function setFiles(files, selected) {
    const field = el("capture");
    const signature = JSON.stringify(files);
    if (field.dataset.signature !== signature) {
        field.dataset.signature = signature;
        field.replaceChildren(...(files.length ? files.map(name => new Option(name, name)) : [new Option("Kayıt yok", "")]));
    }
    if (!captureChanged && selected) field.value = selected;
}

function captureSource() {
    return {file: el("capture").value, host: el("host").value.trim(), port: Number(el("port").value)};
}

function sameSource(first, second) {
    return first && first.file === second.file && first.host === second.host && Number(first.port) === second.port;
}

function legacyCapture() {
    return initialized && !("catalog" in state);
}

function lookupKey(text) {
    return text.trim().normalize("NFD").replace(/\p{M}/gu, "").toUpperCase();
}

function renderSymbolChoices(ready, locked) {
    const field = el("symbol-search"), results = el("symbol-results"), chips = el("symbol-selection");
    const search = lookupKey(field.value);
    field.disabled = locked;
    const listKey = ready ? catalogKey : null;
    if (resultsKey !== listKey || resultsSearch !== search) {
        resultsKey = listKey;
        resultsSearch = search;
        results.replaceChildren();
        if (ready) {
            const matches = availableSymbols.filter(item => lookupKey(item.symbol).includes(search));
            for (const {symbol, port} of matches) {
                const label = document.createElement("label");
                label.className = "symbol-result";
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.value = symbol;
                label.append(checkbox, Object.assign(document.createElement("span"), {textContent: symbol}));
                if (port !== null && port !== undefined) {
                    label.append(Object.assign(document.createElement("span"), {className: "symbol-port", textContent: port}));
                }
                results.append(label);
            }
            if (!matches.length) results.append(Object.assign(document.createElement("p"), {
                className: "symbol-feedback", textContent: availableSymbols.length ? "Eşleşen hisse yok." : "Kayıtta hisse bulunamadı."
            }));
        }
    }
    for (const checkbox of results.querySelectorAll("input")) {
        checkbox.checked = selectedSymbols.has(checkbox.value);
        checkbox.disabled = locked;
    }
    const chipKey = ready && JSON.stringify([...selectedSymbols]);
    if (chipsKey !== chipKey) {
        chipsKey = chipKey;
        chips.replaceChildren();
        if (ready) {
            for (const symbol of selectedSymbols) {
                const item = document.createElement("li");
                const button = document.createElement("button");
                button.type = "button";
                button.className = "symbol-chip";
                button.dataset.removeSymbol = symbol;
                button.setAttribute("aria-label", `${symbol} seçimini kaldır`);
                const close = document.createElement("span");
                close.textContent = "×";
                close.setAttribute("aria-hidden", "true");
                button.append(symbol, close);
                item.append(button);
                chips.append(item);
            }
        }
    }
    for (const button of chips.querySelectorAll("button")) button.disabled = locked;
    chips.hidden = !ready || !selectedSymbols.size;
    results.hidden = !ready;
    el("symbol-search-field").hidden = !ready;
    el("symbol-count-wrap").hidden = !ready;
    value("symbol-count", `${format(selectedSymbols.size)} hisse seçildi`);
}

function renderSymbolPicker() {
    const source = captureSource();
    const catalog = sameSource(state.catalog, source) ? state.catalog : null;
    const ready = catalog?.phase === "ready";
    const sourceValid = source.file && source.host && Number.isInteger(source.port) && source.port >= 1 && source.port <= 65535;
    const locked = requestPending || state.phase === "preparing";
    const legacy = legacyCapture();
    if (ready) {
        const options = catalog.instruments || [];
        const key = JSON.stringify([source.file, source.host, source.port, options]);
        if (catalogKey !== key) {
            catalogKey = key;
            availableSymbols = options.slice().sort((first, second) => first.symbol.localeCompare(second.symbol, "tr"));
            const available = new Set(availableSymbols.map(item => item.symbol));
            const current = sameSource(state, source) ? (state.symbols || []).filter(symbol => available.has(symbol)) : [];
            const defaultSymbol = ["ASELS.E", "ASELS"].find(symbol => available.has(symbol));
            selectedSymbols = new Set(current.length ? current : defaultSymbol ? [defaultSymbol] : []);
            el("symbol-search").value = "";
        }
    }
    const list = el("list-instruments"), load = el("load");
    el("source-actions").hidden = legacy;
    list.disabled = legacy || !initialized || !sourceValid || locked || state.catalog?.phase === "loading";
    load.disabled = !sourceValid || locked || (!legacy && (!ready || !selectedSymbols.size));
    renderSymbolChoices(ready, locked);
    const failed = catalog?.phase === "error";
    const unavailable = ready ? (catalog.unavailable_ports || []).map(item => item.port) : [];
    let message = "Kaydı ve kanalı seçip hisseleri listele.";
    if (legacy) message = "Kaydı ASELS için açabilirsin.";
    else if (failed) message = catalog.error || "Hisseler listelenemedi.";
    else if (catalog?.phase === "loading") message = "Hisse tanımları taranıyor…";
    else if (state.catalog?.phase === "loading") message = "Önceki kaynak taranıyor. Bittiğinde bu kaynağı listeleyebilirsin.";
    else if (ready) {
        const selectionNote = selectedSymbols.size ? "" : "Kaydı yüklemek için en az bir hisse işaretle.";
        const sourceNote = unavailable.length ? `UDP ${unavailable.join(", ")} kanallarını okurken hata oluştu.` : "";
        message = [selectionNote, sourceNote].filter(Boolean).join(" ");
    }
    const status = el("symbol-feedback");
    value("symbol-feedback", message);
    status.hidden = !message;
    status.classList.toggle("negative", failed);
    status.classList.toggle("symbol-warning", unavailable.length > 0);
}

function renderInstruments() {
    const instruments = state.instruments?.length ? state.instruments : (state.symbols || []).map(symbol => ({symbol}));
    const idle = state.phase === "idle";
    const focus = !idle ? state.focus_symbol || instruments[0]?.symbol : legacyCapture() ? "ASELS" : "";
    if (focus !== previousFocus) {
        previousFocus = focus;
        previousBook = previousJournal = undefined;
        if (tipButton && el("journal-rows").contains(tipButton)) closeTip();
    }
    value("instrument-title", focus || "Pay Piyasası");
    el("instrument-tag").hidden = !focus;
    const panel = el("instruments-panel"), body = el("instrument-rows");
    panel.hidden = idle || !instruments.length;
    const routes = sameSource(state.catalog, state) ? state.catalog.instruments || [] : [];
    const key = JSON.stringify(instruments.map(item => item.symbol));
    if (clusterKey !== key) {
        clusterKey = key;
        instrumentRows = new Map();
        body.replaceChildren();
        for (const {symbol} of instruments) {
            const row = body.insertRow();
            const heading = cell(row, "");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "focus-instrument";
            button.dataset.symbol = symbol;
            button.textContent = symbol;
            heading.append(button);
            instrumentRows.set(symbol, {row, button, source: cell(row, "—"), position: cell(row, "—"),
                pnl: cell(row, "—"), fills: cell(row, "—")});
        }
    }
    for (const item of instruments) {
        const record = instrumentRows.get(item.symbol);
        const selected = focus === item.symbol;
        record.row.dataset.focused = String(selected);
        record.button.setAttribute("aria-pressed", String(selected));
        record.button.disabled = requestPending || state.phase === "preparing";
        record.source.textContent = item.source_port ?? item.port ?? routes.find(entry => entry.symbol === item.symbol)?.port ?? "—";
        record.position.textContent = format(item.position);
        record.pnl.textContent = lira(item.total_pnl);
        record.pnl.classList.toggle("positive", item.total_pnl > 0);
        record.pnl.classList.toggle("negative", item.total_pnl < 0);
        record.fills.textContent = format(item.fills);
    }
}

function findStrategy(id) {
    return strategyCatalog?.strategies.find(item => item.id === id);
}

function parameterControl(key, schema) {
    const field = document.createElement("div");
    field.className = "field";
    const heading = document.createElement("span");
    heading.className = "field-label";
    const label = document.createElement("label");
    const id = `parameter-${parameterFields.size}`;
    label.htmlFor = id;
    label.textContent = schema.unit ? `${schema.label || key} (${schema.unit})` : schema.label || key;
    heading.append(label, makeInfo(schema.help || "Bu strateji parametresinin koşuda kullanılan değeri."));

    const options = schema.type === "enum" ? schema.choices : null;
    const input = document.createElement(options ? "select" : "input");
    input.id = id;
    input.name = key;
    if (options) {
        input.required = true;
        options.forEach((option, index) => input.add(new Option(option, String(index))));
    } else if (schema.type === "boolean") {
        input.type = "checkbox";
        field.classList.add("checkbox-field");
    } else {
        const numeric = ["integer", "number"].includes(schema.type);
        input.type = numeric ? "number" : "text";
        input.required = numeric;
        if (numeric) {
            input.step = schema.type === "integer" ? "1" : "any";
            if (schema.minimum !== undefined) input.min = schema.minimum;
            if (schema.maximum !== undefined) input.max = schema.maximum;
        }
    }
    field.append(heading, input);
    return {field, input, options, defaultValue: schema.default};
}

function showParameters(strategy, values = null) {
    const group = el("strategy-parameters");
    const key = strategy && `${strategy.id}:${strategy.version}`;
    if (parameterKey !== key) {
        if (tipButton && group.contains(tipButton)) closeTip();
        parameterKey = key;
        parameterFields = new Map();
        group.replaceChildren();
        for (const [name, schema] of Object.entries(strategy?.parameters || {})) {
            const control = parameterControl(name, schema);
            parameterFields.set(name, control);
            group.append(control.field);
        }
    }
    if (values !== null) {
        for (const [name, {input, options, defaultValue}] of parameterFields) {
            const setting = values[name] ?? defaultValue;
            if (options) input.selectedIndex = options.indexOf(setting);
            else if (input.type === "checkbox") input.checked = setting === true;
            else input.value = setting ?? "";
        }
    }
    el("parameter-section").hidden = parameterFields.size === 0;
    value("strategy-description", strategy?.description || "");
    el("strategy-description").hidden = !strategy?.description;
}

function renderStrategySettings() {
    const selection = state.strategy_selection || {};
    const selector = el("strategy-select");
    const applied = findStrategy(selection.id);
    value("strategy-active-name", applied?.name || selection.id || "—");
    value("strategy-active-key", selection.id && selection.version ? `${selection.id} · ${selection.version}` : "");
    el("strategy-active-key").hidden = !selection.id || !selection.version;

    const editablePhase = ["idle", "ready"].includes(state.phase);
    if (strategyCatalog && !settingsChanged) {
        selector.value = applied?.id || strategyCatalog.default_strategy_id;
        showParameters(findStrategy(selector.value), selection.params || {});
        for (const input of el("shared-config").querySelectorAll("input")) {
            input.value = state.config?.[input.name] ?? "";
        }
    }
    const locked = !strategyCatalog || !editablePhase || requestPending;
    selector.disabled = locked;
    for (const {input} of parameterFields.values()) input.disabled = locked;
    for (const input of el("shared-config").querySelectorAll("input")) input.disabled = locked;
    el("save-config").disabled = locked || !selector.value || !settingsChanged;
    el("discard-config").hidden = !settingsChanged;
    el("discard-config").disabled = requestPending;
    el("retry-strategies").hidden = !strategyListError;
    const runError = state.strategy_error;
    const runErrorContext = runError && `${runError.id} stratejisi ITCH sıra ${format(runError.sequence)} sırasında durdu. Yeni koşu için replay’i başa al.`;
    const message = strategyListError || strategyError || runErrorContext || (!editablePhase && strategyCatalog && state.phase !== "preparing" ?
        "Strateji ve ayarları değiştirmek için önce replay’i başa al." :
        settingsChanged ? "Değişiklikleri replay başlamadan önce uygula veya taslağı bırak." : "");
    value("strategy-feedback", message);
    el("strategy-feedback").hidden = !message;
    el("strategy-feedback").classList.toggle("negative", Boolean(strategyListError || strategyError || runErrorContext));
}

async function loadStrategies() {
    if (strategyListPending) return;
    strategyListPending = true;
    strategyListError = "";
    renderStrategySettings();
    const selector = el("strategy-select");
    selector.replaceChildren(new Option("Stratejiler yükleniyor…", ""));
    try {
        const response = await fetch("/api/strategies", {cache: "no-store"});
        if (!response.ok) throw new Error();
        const catalog = await response.json();
        if (!Array.isArray(catalog.strategies) || !catalog.strategies.some(item => item.id === catalog.default_strategy_id)) {
            throw new Error();
        }
        strategyCatalog = catalog;
        selector.replaceChildren(...catalog.strategies.map(item => new Option(item.name, item.id)));
    } catch {
        selector.replaceChildren(new Option("Stratejiler yüklenemedi", ""));
        strategyListError = "Stratejiler yüklenemedi. Yerel sunucuyu kontrol et ve listeyi yeniden yükle.";
    } finally {
        strategyListPending = false;
        renderStrategySettings();
    }
}

function render(next) {
    state = next;
    const {phase, progress = {}, strategy = {}, files = []} = state;
    if (!["idle", "ready", "preparing"].includes(phase)) settingsChanged = false;
    setFiles(files, state.file);
    if (!initialized || !captureChanged) {
        el("host").value = state.host || "";
        el("port").value = state.port || "";
    }
    initialized = true;
    value("connection", "Yerel bağlantı");
    el("connection-dot").className = "signal online";
    value("active-file", state.file || "Kayıt seçilmedi");
    value("phase", phase === "preparing" ? `Hazırlanıyor · %${format(progress.preparation_percent)}` : state.seeking_open ? "Sürekli işleme ilerliyor" : phases[phase] || "Bekliyor");
    el("phase-badge").dataset.phase = phase;
    value("market-status", marketPhases[state.market_phase] || state.market_phase || "—");
    value("market-time", progress.market_time || "--:--:--");
    value("events", format(progress.events));
    value("sequence", progress.events ? format(progress.sequence) : "—");
    value("actual-speed", phase === "running" ? `${format(progress.actual_speed)}×` : "—");
    value("start-label", phase === "paused" ? "Devam et" : "Başlat");
    el("start").disabled = !["ready", "paused"].includes(phase) || requestPending || (phase === "ready" && settingsChanged);
    el("pause").disabled = phase !== "running" || requestPending;
    el("reset").disabled = phase === "idle" || requestPending;
    el("advance-open").disabled = !["ready", "paused"].includes(phase) || state.market_phase === "P_SUREKLI_ISLEM" || requestPending || (phase === "ready" && settingsChanged);
    renderSymbolPicker();
    renderInstruments();
    el("strategy-toggle").checked = Boolean(state.strategy_enabled);
    el("strategy-toggle").disabled = requestPending;
    value("strategy-status", state.strategy_enabled ? "Açık" : "Kapalı");
    renderStrategySettings();
    for (const button of document.querySelectorAll("[data-speed]")) button.setAttribute("aria-pressed", String(String(state.speed) === button.dataset.speed));
    if (state.speed !== "max" && !speedChanged) el("custom-speed").value = state.speed;
    const error = actionError || state.error;
    value("error", error || "");
    el("error").hidden = !error;
    balance("total-pnl", strategy.total_pnl);
    balance("realized-pnl", strategy.realized_pnl);
    balance("unrealized-pnl", strategy.unrealized_pnl);
    value("inventory", format(strategy.inventory));
    value("average-price", lira(strategy.average_price));
    value("cash", lira(strategy.cash));
    value("buy-price", lira(strategy.buy_price));
    value("sell-price", lira(strategy.sell_price));
    value("bid-ahead", format(strategy.bid_ahead));
    value("ask-ahead", format(strategy.ask_ahead));
    value("fill-count", format(strategy.fills));
    value("ouch-sent", format(strategy.ouch_sent));
    value("ouch-received", format(strategy.ouch_received));
    bookRows();
    matchingSummary(state.matching_audit);
    journalRows(state.journal);
    chart();
}

async function poll() {
    if (requestPending || pollPending) return;
    pollPending = true;
    const revision = actionRevision;
    try {
        const response = await fetch("/api/state", {cache: "no-store"});
        if (!response.ok) throw new Error("Dashboard yanıt vermiyor.");
        const next = await response.json();
        if (!requestPending && revision === actionRevision) render(next);
    } catch (error) {
        value("connection", "Bağlantı kesildi");
        el("connection-dot").className = "signal offline";
    } finally {
        pollPending = false;
    }
}

async function control(message) {
    if (requestPending) return;
    requestPending = true;
    actionRevision += 1;
    actionError = "";
    if (message.action === "select_strategy") strategyError = "";
    renderStrategySettings();
    renderSymbolPicker();
    renderInstruments();
    try {
        const response = await fetch("/api/control", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(message)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Kontrol işlemi başarısız.");
        if (message.action === "load") captureChanged = false;
        if (["select_strategy", "reset"].includes(message.action)) {
            settingsChanged = false;
            strategyError = "";
        }
        if (message.action === "speed") speedChanged = false;
        requestPending = false;
        render(data);
    } catch (error) {
        if (message.action === "select_strategy") strategyError = error.message;
        else actionError = error.message;
        requestPending = false;
        render(state);
    }
}

el("capture-form").addEventListener("input", event => {
    if (["capture", "host", "port"].includes(event.target.id)) {
        captureChanged = true;
        renderSymbolPicker();
    }
});
el("capture-form").addEventListener("submit", event => {
    event.preventDefault();
    const id = event.submitter?.id;
    if (id === "list-instruments" && !el(id).disabled) {
        control({action: "list_instruments", ...captureSource()});
    } else if (id === "load" && !el(id).disabled) {
        const payload = legacyCapture() ? {} : {symbols: [...selectedSymbols]};
        control({action: "load", ...captureSource(), ...payload});
    }
});
el("symbol-search").addEventListener("input", renderSymbolPicker);
el("symbol-search").addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
el("symbol-results").addEventListener("change", event => {
    if (event.target.type !== "checkbox") return;
    if (event.target.checked) selectedSymbols.add(event.target.value);
    else selectedSymbols.delete(event.target.value);
    renderSymbolPicker();
});
el("symbol-selection").addEventListener("click", event => {
    const button = event.target.closest("[data-remove-symbol]");
    if (!button) return;
    selectedSymbols.delete(button.dataset.removeSymbol);
    renderSymbolPicker();
    el("symbol-search").focus();
});
el("instrument-rows").addEventListener("click", event => {
    const button = event.target.closest("[data-symbol]");
    if (button && button.dataset.symbol !== state.focus_symbol) control({action: "focus_instrument", symbol: button.dataset.symbol});
});
for (const action of ["start", "pause", "reset"]) el(action).addEventListener("click", () => control({action}));
el("advance-open").addEventListener("click", () => control({action: "advance_open"}));
el("book-depth").addEventListener("change", bookRows);
el("book-source").addEventListener("change", bookRows);
for (const button of document.querySelectorAll("[data-speed]")) button.addEventListener("click", () => {
    const speed = button.dataset.speed;
    control({action: "speed", value: speed === "max" ? speed : Number(speed)});
});
el("custom-speed").addEventListener("input", () => { speedChanged = true; });
el("speed-form").addEventListener("submit", event => {
    event.preventDefault();
    control({action: "speed", value: Number(el("custom-speed").value)});
});
el("strategy-toggle").addEventListener("change", event => control({action: "strategy", enabled: event.target.checked}));
el("retry-strategies").addEventListener("click", loadStrategies);
el("discard-config").addEventListener("click", () => {
    settingsChanged = false;
    strategyError = "";
    render(state);
});
el("strategy-select").addEventListener("change", event => {
    const strategy = findStrategy(event.target.value);
    const selection = state.strategy_selection;
    showParameters(strategy, strategy && strategy.id === selection?.id ? selection.params || {} : {});
    settingsChanged = true;
    strategyError = "";
    render(state);
});
el("config-form").addEventListener("input", event => {
    if (event.target === el("strategy-select")) return;
    settingsChanged = true;
    strategyError = "";
    render(state);
});
el("config-form").addEventListener("submit", event => {
    event.preventDefault();
    if (el("save-config").disabled || !event.currentTarget.reportValidity()) return;
    const config = Object.fromEntries([...el("shared-config").querySelectorAll("input")].map(input => [input.name, input.valueAsNumber]));
    const params = Object.fromEntries([...parameterFields].map(([name, {input, options}]) => {
        if (options) return [name, options[input.selectedIndex]];
        if (input.type === "checkbox") return [name, input.checked];
        return [name, input.type === "number" ? input.valueAsNumber : input.value];
    }));
    control({action: "select_strategy", strategy_id: el("strategy-select").value, params, config});
});
for (const button of document.querySelectorAll("[data-series]")) button.addEventListener("click", () => {
    chartSeries = button.dataset.series;
    for (const entry of document.querySelectorAll("[data-series]")) entry.setAttribute("aria-pressed", String(chartSeries === entry.dataset.series));
    chart();
});
new ResizeObserver(chart).observe(el("chart-area"));
loadStrategies();
poll();
setInterval(poll, 350);
