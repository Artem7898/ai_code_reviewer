const messages = {
    eyebrow: ["Персональный помощник разработчика", "Personal developer assistant"],
    logout: ["Выйти", "Sign out"],
    welcome: ["Войдите, чтобы запустить ревью.", "Sign in to start a review."],
    signInTitle: ["Вход", "Sign in"],
    authHelp: ["Используйте учётную запись API. Сессия действует до закрытия или обновления страницы.", "Use your API account. Closing or reloading the page ends the session."],
    username: ["Имя пользователя", "Username"],
    password: ["Пароль", "Password"],
    signIn: ["Войти", "Sign in"],
    analysisSettings: ["Настройки анализа", "Analysis settings"],
    loadingConfig: ["Загрузка конфигурации…", "Loading configuration…"],
    newReview: ["Новое ревью", "New review"],
    singleFile: ["Отдельный файл", "Single file"],
    sourceFile: ["Исходный код или PDF", "Source code or PDF"],
    reviewFile: ["Проверить файл", "Review file"],
    localProject: ["Локальный проект", "Local project"],
    allowedProject: ["Проект в разрешённом каталоге", "Project in the allowed directory"],
    chooseProject: ["Выберите проект", "Select a project"],
    reviewScope: ["Область анализа", "Review scope"],
    gitDiff: ["Изменения Git", "Git changes"],
    fullProject: ["Весь проект", "Entire project"],
    baseRevision: ["Базовая ревизия Git", "Base Git revision"],
    baseHelp: ["Сравнение с HEAD включает изменения рабочего дерева. Выберите другую ревизию для сравнения веток.", "Comparing with HEAD includes working tree changes. Choose another revision to compare branches."],
    proposeChanges: ["Предложить изменения", "Propose changes"],
    proposalHelp: ["Результат сохраняется как отдельный артефакт для проверки. Исходные файлы проекта не изменяются.", "The proposal is saved as a separate artifact for review. Project source files stay intact."],
    task: ["Задача", "Task"],
    migrate: ["Модернизация кода", "Modernize code"],
    generateTests: ["Генерация тестов", "Generate tests"],
    projectHelp: ["Используется проект, выбранный выше.", "Uses the project selected above."],
    relativeFile: ["Файл относительно корня проекта", "File relative to the project root"],
    targetStack: ["Целевая платформа", "Target stack"],
    prepareProposal: ["Подготовить предложение", "Prepare proposal"],
    runReview: ["Запустить ревью", "Start review"],
    result: ["Результат", "Result"],
    refresh: ["Обновить", "Refresh"],
    chooseResult: ["Выберите задание или отчёт из истории.", "Select a job or a report from the history."],
    jobs: ["Задания", "Jobs"],
    emptyJobs: ["Заданий пока нет.", "No jobs yet."],
    reportHistory: ["История отчётов", "Report history"],
    emptyReports: ["Отчётов пока нет.", "No reports yet."],
    previous: ["Назад", "Previous"],
    next: ["Вперёд", "Next"],
    footer: ["Проверяйте замечания и изменения перед применением. Ответы модели могут содержать ошибки.", "Review findings and changes before applying them. Model responses can contain errors."],
    language: ["Язык интерфейса", "Interface language"],
    queued: ["В очереди", "Queued"],
    pending: ["Ожидает", "Pending"],
    running: ["Выполняется", "Running"],
    completed: ["Завершено", "Completed"],
    failed: ["Ошибка", "Failed"],
    cancelling: ["Отменяется", "Cancelling"],
    cancelled: ["Отменено", "Cancelled"],
    requestFailed: ["Не удалось выполнить запрос.", "The request failed."],
    invalidData: ["Некорректные данные", "Invalid data"],
    requestRejected: ["Запрос отклонён сервером.", "The server rejected the request."],
    signInRequired: ["Войдите в систему.", "Sign in first."],
    sessionExpired: ["Сессия истекла. Войдите снова.", "Your session has expired. Sign in again."],
    missingToken: ["Сервер не вернул токен доступа.", "The server did not return an access token."],
    signedIn: ["Вход выполнен. Подготовьте файл или выберите проект.", "Signed in. Choose a file or a project."],
    signedOut: ["Вы вышли из системы.", "Signed out."],
    noProjects: ["Разрешённые проекты пока не зарегистрированы. Можно загрузить отдельный файл.", "No allowed projects are registered. You can upload a file."],
    jobQueued: ["Задание добавлено в очередь. Прогресс обновляется автоматически.", "Job queued. Progress updates automatically."],
    chooseFile: ["Выберите файл.", "Select a file."],
    fileTooLarge: ["Размер файла превышает разрешённый лимит.", "The file exceeds the size limit."],
    projectRequired: ["Сначала выберите проект.", "Select a project first."],
    job: ["Задание", "Job"],
    review: ["Ревью", "Review"],
    report: ["Отчёт", "Report"],
    finding: ["Замечание", "Finding"],
    noFindings: ["Замечаний не обнаружено.", "No findings."],
    truncatedFindings: ["Показаны первые 200 замечаний. Полный результат доступен в SARIF.", "The first 200 findings are shown. Download SARIF for the full result."],
    progress: ["Прогресс задания", "Job progress"],
    cancelJob: ["Отменить задание", "Cancel job"],
    cancelRequested: ["Запрошена отмена задания.", "Cancellation requested."],
    openArtifact: ["Открыть предложенные изменения", "Open proposed changes"],
    artifact: ["Предложенный артефакт", "Proposed artifact"],
    downloadJson: ["Скачать JSON", "Download JSON"],
    artifactOpened: ["Предложение открыто", "Proposal opened"],
    downloadSarif: ["Скачать SARIF", "Download SARIF"],
    emptyReport: ["Нет содержимого.", "No content."],
    refreshed: ["Данные обновлены.", "Data refreshed."],
    defaultModel: ["модель по умолчанию", "default model"],
    readOnly: ["Проекты доступны только для чтения", "Projects are read-only"],
    applySeparately: ["Изменения требуют отдельного применения", "Changes must be applied separately"],
    serverLimit: ["по настройкам сервера", "server default"],
    providerInfo: ["Провайдер: {0} · Модель: {1} · {2}", "Provider: {0} · Model: {1} · {2}"],
    size: ["{0} МиБ", "{0} MiB"],
    limits: ["Лимит файла: {0}. Файлов в задании: {1}.", "File limit: {0}. Files per job: {1}."],
    validation: ["Проверка артефакта: {0}", "Artifact validation: {0}"],
    unknown: ["неизвестно", "unknown"],
    upload: ["Проверка файла", "File review"],
    scan: ["Проверка проекта", "Project review"],
};
const storageKey = "reviewer-language";
let language = "ru";
try {
    if (localStorage.getItem(storageKey) === "en") language = "en";
} catch { /* Storage may be disabled. */ }

const bindings = new WeakMap();

export function translate(key, ...values) {
    return messages[key][language === "en" ? 1 : 0].replace(/\{(\d+)\}/g, (_, index) => values[index]);
}

export function label(key) {
    return () => translate(key);
}

export function currentLocale() {
    return language === "en" ? "en-US" : "ru-RU";
}

export function setText(element, value) {
    const node = document.createTextNode(typeof value === "function" ? value() : String(value));
    element.replaceChildren(node);
    if (typeof value === "function") bindings.set(node, value);
}

export function switchLanguage(value = language) {
    language = value === "en" ? "en" : "ru";
    document.documentElement.lang = language;
    document.getElementById("language").value = language;
    try { localStorage.setItem(storageKey, language); } catch { /* Storage may be disabled. */ }

    for (const element of document.querySelectorAll("[data-i18n]")) {
        element.textContent = translate(element.dataset.i18n);
    }
    for (const element of document.querySelectorAll("[data-i18n-label]")) {
        element.setAttribute("aria-label", translate(element.dataset.i18nLabel));
    }
    // Update text in place so existing controls, open artifacts and focus survive.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const render = bindings.get(node);
        if (render) node.nodeValue = render();
    }
}
