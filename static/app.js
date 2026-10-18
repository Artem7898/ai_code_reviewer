// All untrusted model output, filenames and server messages are rendered as text.
// The bearer token exists only in this module's memory, never in Web Storage.
const byId = (id) => document.getElementById(id);
const terminalStates = new Set(["completed", "failed", "cancelled", "succeeded"]);
const statusNames = {
    queued: "В очереди", pending: "Ожидает", running: "Выполняется",
    completed: "Завершено", succeeded: "Завершено", failed: "Ошибка",
    cancelling: "Отменяется", cancelled: "Отменено",
};
const controllers = new Set();
let accessToken = null;
let sessionVersion = 0;
let pollTimer = null;
let refreshPromise = null;
let selectedJobId = null;
let reportOffset = 0;
let reportRequestVersion = 0;
let maxUploadBytes = 0;
let hasActiveJobs = false;
let selectedJobRevision = null;
let selectedSummaryRevision = null;
let selectedDetail = null;
let detailRequest = null;
let previousStatuses = new Map();
const reportPageSize = 20;

// Remove tokens left by older releases without reading or reusing their values.
for (const name of ["localStorage", "sessionStorage"]) {
    try { window[name].removeItem("access_token"); } catch { /* Storage may be disabled. */ }
}

function element(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
}

function message(text, isError = false) {
    const item = byId("message");
    item.textContent = text;
    item.classList.toggle("error", isError);
}

function errorMessage(error) {
    return error instanceof Error ? error.message : "Не удалось выполнить запрос.";
}

function detailMessage(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => item.msg || "Некорректные данные").join("; ");
    return "Запрос отклонён сервером.";
}

async function api(path, options = {}, authenticated = true) {
    const version = sessionVersion;
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
        const headers = new Headers(options.headers);
        if (authenticated) {
            if (!accessToken) throw new Error("Войдите в систему.");
            headers.set("Authorization", `Bearer ${accessToken}`);
        }
        const signal = options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal;
        const response = await fetch(path, {
            ...options, headers, signal,
            credentials: "omit", cache: "no-store", redirect: "error",
        });
        const payload = await response.json().catch(() => ({}));
        if (version !== sessionVersion) throw new DOMException("Session changed", "AbortError");
        if (response.status === 401 && authenticated) {
            endSession();
            throw new Error("Сессия истекла. Войдите снова.");
        }
        if (!response.ok) throw new Error(detailMessage(payload.detail));
        return payload;
    } finally {
        clearTimeout(timeout);
        controllers.delete(controller);
    }
}

function stopPolling() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
}

function endSession() {
    sessionVersion += 1;
    accessToken = null;
    selectedJobId = null;
    reportOffset = 0;
    reportRequestVersion += 1;
    hasActiveJobs = false;
    selectedJobRevision = null;
    selectedSummaryRevision = null;
    selectedDetail = null;
    detailRequest?.controller.abort();
    detailRequest = null;
    previousStatuses.clear();
    stopPolling();
    for (const controller of controllers) controller.abort();
    controllers.clear();
    refreshPromise = null;
    byId("workspace").hidden = true;
    byId("logout").hidden = true;
    byId("auth-panel").hidden = false;
    byId("password").value = "";
    byId("upload-file").value = "";
    byId("selected-result").replaceChildren(element("p", "Выберите задание или отчёт из истории.", "muted"));
    byId("job-list").replaceChildren();
    byId("report-list").replaceChildren();
    byId("project-name").replaceChildren(element("option", "Выберите проект"));
    byId("project-name").firstElementChild.value = "";
}

function schedulePolling() {
    stopPolling();
    if (accessToken && hasActiveJobs && !document.hidden) {
        pollTimer = setTimeout(() => {
            refresh().catch(showError).finally(schedulePolling);
        }, 2500);
    }
}

function showError(error) {
    if (error?.name !== "AbortError") message(errorMessage(error), true);
}

async function withForm(form, callback) {
    const button = form.querySelector('button[type="submit"]');
    const version = sessionVersion;
    button.disabled = true;
    try { await callback(); }
    catch (error) {
        if (version === sessionVersion || !accessToken) showError(error);
    } finally { button.disabled = false; }
}

byId("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withForm(event.currentTarget, async () => {
        const body = new URLSearchParams(new FormData(event.currentTarget));
        const result = await api("/token", { method: "POST", body }, false);
        if (typeof result.access_token !== "string") throw new Error("Сервер не вернул токен доступа.");
        sessionVersion += 1;
        accessToken = result.access_token;
        byId("password").value = "";
        byId("auth-panel").hidden = true;
        byId("workspace").hidden = false;
        byId("logout").hidden = false;
        message("Вход выполнен. Подготовьте файл или выберите проект.");
        await loadConfiguration();
        await Promise.all([refresh(), loadReports()]);
        schedulePolling();
    });
});

byId("logout").addEventListener("click", () => {
    endSession();
    message("Вы вышли из системы.");
});

async function loadConfiguration() {
    const config = await api("/api/config");
    const provider = config.provider || "local";
    const model = config.model || "модель по умолчанию";
    byId("provider-info").textContent = `Провайдер: ${provider} · Модель: ${model} · ${config.read_only ? "Проекты доступны только для чтения" : "Изменения требуют отдельного применения"}`;
    maxUploadBytes = Number(config.limits?.max_upload_bytes) || 0;
    const size = maxUploadBytes ? `${(maxUploadBytes / 1024 / 1024).toFixed(1)} МиБ` : "по настройкам сервера";
    byId("limits-info").textContent = `Лимит файла: ${size}. Файлов в задании: ${config.limits?.max_files || "по настройкам сервера"}.`;
    const select = byId("project-name");
    select.replaceChildren();
    const prompt = element("option", "Выберите проект");
    prompt.value = "";
    select.append(prompt);
    for (const project of (config.projects || [])) {
        const name = typeof project === "string" ? project : project.name;
        const option = element("option", name);
        option.value = name;
        select.append(option);
    }
    if (!config.projects?.length) message("Разрешённые проекты пока не зарегистрированы. Можно загрузить отдельный файл.");
}

async function enqueue(path, body) {
    const job = await api(path, { method: "POST", body });
    selectedJobId = job.id;
    selectedJobRevision = null;
    hasActiveJobs = true;
    await showSelectedJob(job);
    message("Задание добавлено в очередь. Прогресс обновляется автоматически.");
    await refresh();
    schedulePolling();
}

byId("upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withForm(event.currentTarget, async () => {
        const file = byId("upload-file").files[0];
        if (!file) throw new Error("Выберите файл.");
        if (maxUploadBytes && file.size > maxUploadBytes) throw new Error("Размер файла превышает разрешённый лимит.");
        const body = new FormData();
        body.append("file", file);
        await enqueue("/api/upload-and-review", body);
        byId("upload-file").value = "";
    });
});

byId("review-mode").addEventListener("change", () => {
    byId("base-ref").disabled = byId("review-mode").value !== "diff";
});

byId("project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withForm(event.currentTarget, () => enqueue("/api/scan-local-project", new FormData(event.currentTarget)));
});

byId("proposal-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withForm(event.currentTarget, async () => {
        const project = byId("project-name").value;
        if (!project) throw new Error("Сначала выберите проект.");
        const body = new FormData(event.currentTarget);
        const kind = body.get("kind");
        body.delete("kind");
        body.append("project_name", project);
        await enqueue(`/api/${kind}`, body);
    });
});

function timestamp(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("ru-RU");
}

function renderJobList(jobs) {
    const list = byId("job-list");
    const fragment = document.createDocumentFragment();
    for (const job of jobs.slice(0, 50)) {
        const button = element("button", `${job.project_name || job.kind || "Задание"} · ${statusNames[job.status] || job.status}`, "item-button");
        button.type = "button";
        button.append(element("span", timestamp(job.created_at)));
        button.addEventListener("click", async () => {
            selectedJobId = job.id;
            selectedJobRevision = null;
            try {
                await showSelectedJob(job, true);
            } catch (error) { showError(error); }
        });
        fragment.append(button);
    }
    list.replaceChildren(fragment);
    if (!jobs.length) list.append(element("p", "Заданий пока нет.", "muted"));
}

function summaryRevision(job) {
    return JSON.stringify([job.id, job.status, job.progress, job.updated_at]);
}

async function selectedJobDetail(job) {
    const key = summaryRevision(job);
    if (selectedDetail?.key === key) return selectedDetail.job;
    if (detailRequest?.key === key) return detailRequest.promise;
    // At most one full-result request is retained, even during rapid selection.
    detailRequest?.controller.abort();
    const controller = new AbortController();
    const request = {
        key, controller,
        promise: api(`/api/jobs/${encodeURIComponent(job.id)}`, { signal: controller.signal }),
    };
    detailRequest = request;
    try {
        const detail = await request.promise;
        if (detailRequest === request && selectedJobId === job.id) selectedDetail = { key, job: detail };
        return detail;
    } finally {
        if (detailRequest === request) detailRequest = null;
    }
}

async function showSelectedJob(job, selected = false) {
    const revision = summaryRevision(job);
    selectedSummaryRevision = revision;
    if (selectedDetail?.key === revision) renderJob(selectedDetail.job);
    else renderJob(job);
    // Poll summaries contain no result. Fetch the full output only when the
    // user selects a job or a completed summary needs its final result.
    if (!selected && !["completed", "succeeded"].includes(job.status)) return;
    const detail = await selectedJobDetail(job);
    if (selectedJobId === job.id && selectedSummaryRevision === revision) renderJob(detail);
}

async function refresh() {
    if (!accessToken) return;
    if (refreshPromise) return refreshPromise;
    const version = sessionVersion;
    const pending = (async () => {
        const payload = await api("/api/jobs?limit=50");
        const jobs = Array.isArray(payload) ? payload : payload.items || [];
        hasActiveJobs = jobs.some((job) => !terminalStates.has(job.status));
        const newlyCompleted = jobs.some((job) => (
            previousStatuses.has(job.id) && previousStatuses.get(job.id) !== job.status
            && terminalStates.has(job.status)
        ));
        previousStatuses = new Map(jobs.slice(0, 50).map((job) => [job.id, job.status]));
        renderJobList(jobs);
        if (selectedJobId) {
            const job = jobs.find((item) => item.id === selectedJobId);
            if (job) await showSelectedJob(job);
        }
        if (newlyCompleted) await loadReports();
    })();
    refreshPromise = pending;
    try { await pending; }
    finally { if (version === sessionVersion && refreshPromise === pending) refreshPromise = null; }
}

function renderFindings(result, parent) {
    if (result.summary) parent.append(element("p", result.summary));
    const findings = Array.isArray(result.findings) ? result.findings : [];
    if (!findings.length && result.findings) parent.append(element("p", "Замечаний не обнаружено.", "muted"));
    for (const finding of findings.slice(0, 200)) {
        const article = element("article", null, "finding");
        article.append(element("h3", `${finding.severity || "info"} · ${finding.title || finding.rule_id || "Замечание"}`));
        const line = finding.line_start || finding.start_line || finding.line;
        const location = `${finding.file_path || finding.path || ""}${line ? `:${line}` : ""}`;
        if (location) article.append(element("p", location, "result-meta"));
        if (finding.explanation || finding.message || finding.description) {
            article.append(element("p", finding.explanation || finding.message || finding.description));
        }
        if (finding.evidence) article.append(element("pre", typeof finding.evidence === "string" ? finding.evidence : JSON.stringify(finding.evidence, null, 2)));
        if (finding.suggestion) article.append(element("p", finding.suggestion));
        parent.append(article);
    }
    if (findings.length > 200) parent.append(element("p", "Показаны первые 200 замечаний. Полный результат доступен в SARIF.", "muted"));
}

function downloadJson(value, filename) {
    const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = element("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderJob(job) {
    const revision = JSON.stringify([job.id, job.status, job.progress, job.error, job.result]);
    if (revision === selectedJobRevision) return;
    selectedJobRevision = revision;
    const parent = byId("selected-result");
    parent.replaceChildren(element("h3", `${job.kind || "Ревью"} · ${statusNames[job.status] || job.status}`));
    parent.append(element("p", `Задание ${job.id} · ${timestamp(job.created_at)}`, "result-meta"));
    const progress = element("progress");
    progress.max = 1;
    progress.value = Math.max(0, Math.min(1, Number(job.progress) || 0));
    progress.setAttribute("aria-label", "Прогресс задания");
    parent.append(progress);
    const actions = element("div", null, "result-actions");
    if (!terminalStates.has(job.status)) {
        const cancel = element("button", "Отменить задание", "danger");
        cancel.type = "button";
        cancel.addEventListener("click", async () => {
            cancel.disabled = true;
            try {
                await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
                await refresh();
                message("Запрошена отмена задания.");
            } catch (error) { showError(error); }
            finally { cancel.disabled = false; }
        });
        actions.append(cancel);
    }
    if (job.error) parent.append(element("p", job.error, "message error"));
    let result = job.result;
    if (typeof result === "string") {
        try { result = JSON.parse(result); } catch { parent.append(element("pre", result)); result = null; }
    }
    if (result && typeof result === "object") {
        renderFindings(result, parent);
        if (result.validation) {
            parent.append(element("p", `Проверка артефакта: ${result.validation.status || "неизвестно"}`, "result-meta"));
            if (result.validation.output) parent.append(element("pre", result.validation.output));
        }
        if (result.artifact_id) {
            let artifactLoaded = false;
            const artifact = element("button", "Открыть предложенные изменения", "secondary");
            artifact.type = "button";
            artifact.addEventListener("click", async () => {
                artifact.disabled = true;
                try {
                    const payload = await api(`/api/jobs/${encodeURIComponent(job.id)}/artifacts`);
                    if (selectedJobId !== job.id) return;
                    const details = element("details");
                    details.open = true;
                    details.append(element("summary", "Предложенный артефакт"));
                    details.append(element("pre", JSON.stringify(payload, null, 2)));
                    const download = element("button", "Скачать JSON", "secondary");
                    download.type = "button";
                    download.addEventListener("click", () => downloadJson(payload, `proposal-${job.id}.json`));
                    details.append(download);
                    parent.append(details);
                    artifactLoaded = true;
                    artifact.textContent = "Предложение открыто";
                } catch (error) { showError(error); }
                finally { artifact.disabled = artifactLoaded; }
            });
            actions.append(artifact);
        }
        if (Array.isArray(result.findings) || result.sarif) {
            const sarif = element("button", "Скачать SARIF", "secondary");
            sarif.type = "button";
            sarif.addEventListener("click", async () => {
                sarif.disabled = true;
                try {
                    downloadJson(await api(`/api/jobs/${encodeURIComponent(job.id)}/sarif`), `review-${job.id}.sarif`);
                } catch (error) { showError(error); }
                finally { sarif.disabled = false; }
            });
            actions.append(sarif);
        }
    }
    parent.append(actions);
}

async function loadReports() {
    const requestedOffset = reportOffset;
    const requestVersion = ++reportRequestVersion;
    const reports = await api(`/api/reports?skip=${requestedOffset}&limit=${reportPageSize}`);
    if (requestVersion !== reportRequestVersion) return;
    const items = Array.isArray(reports) ? reports : reports.items || [];
    const list = byId("report-list");
    list.replaceChildren();
    for (const report of items) {
        const button = element("button", report.project_name || report.file_path || "Отчёт", "item-button");
        button.type = "button";
        button.append(element("span", `${report.file_path || ""} · ${statusNames[report.status] || report.status} · ${timestamp(report.created_at)}`));
        button.addEventListener("click", () => {
            selectedJobId = null;
            selectedJobRevision = null;
            selectedSummaryRevision = null;
            selectedDetail = null;
            detailRequest?.controller.abort();
            detailRequest = null;
            const parent = byId("selected-result");
            parent.replaceChildren(element("h3", report.project_name || "Отчёт"));
            parent.append(element("p", `${report.file_path || ""} · ${timestamp(report.created_at)}`, "result-meta"));
            try {
                const result = JSON.parse(report.review_result);
                if (result && typeof result === "object") renderFindings(result, parent);
                else parent.append(element("pre", report.review_result, "report-text"));
            } catch { parent.append(element("pre", report.review_result || report.summary || "Нет содержимого.", "report-text")); }
        });
        list.append(button);
    }
    if (!items.length) list.append(element("p", "Отчётов пока нет.", "muted"));
    byId("reports-previous").disabled = requestedOffset === 0;
    byId("reports-next").disabled = items.length < reportPageSize;
    byId("reports-page").textContent = String(Math.floor(requestedOffset / reportPageSize) + 1);
}

byId("reports-previous").addEventListener("click", () => {
    reportOffset = Math.max(0, reportOffset - reportPageSize);
    loadReports().catch(showError);
});
byId("reports-next").addEventListener("click", () => {
    reportOffset += reportPageSize;
    loadReports().catch(showError);
});
byId("refresh").addEventListener("click", async () => {
    const button = byId("refresh");
    button.disabled = true;
    try {
        await Promise.all([refresh(), loadReports()]);
        message("Данные обновлены.");
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
});
document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (accessToken) refresh().catch(showError).finally(schedulePolling);
});
window.addEventListener("pagehide", endSession);
