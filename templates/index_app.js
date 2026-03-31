const { useEffect, useMemo, useRef, useState } = React;
const { createRoot } = ReactDOM;
const { MemoryRouter, Routes, Route, useNavigate } = ReactRouterDOM;

function isoDate(date) {
    const year = date.getFullYear();
    const month = `${date.getMonth() + 1}`.padStart(2, "0");
    const day = `${date.getDate()}`.padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function shiftDays(days) {
    const date = new Date();
    date.setDate(date.getDate() + days);
    return date;
}

function lastDaysRange(days) {
    return {
        date_from: isoDate(shiftDays(-(days - 1))),
        date_to: isoDate(new Date()),
    };
}

const DEFAULT_SETTINGS = {
    email: "",
    auth_code: "",
    api_key: "",
    save_path: "",
    company: "",
    remember_settings: true,
};

const DEFAULT_RUN_SETTINGS = {
    ...lastDaysRange(30),
    quick_range: "last_30_days",
};

const DEFAULT_PROGRESS = {
    progress: 0,
    status_text: "等待任务开始...",
    logs: [],
    stats: { emails: 0, invoices: 0, errors: 0 },
    is_running: false,
    run_state: "idle",
    last_error: "",
    stop_requested: false,
    can_stop: false,
    quota_exhausted: false,
    quota_message: "",
    build_identity: null,
};

const APP_BRAND = {
    name: "InvoiceFlowAI",
    subtitle: "AI发票管家",
};

const APP_VISIBLE_COPY = {
    topSubtitle: "AI发票管家",
    footerVersion: "InvoiceFlowAI v2026.03.31",
    footerStamp: "P-H-Dx",
    githubLabel: "产品官网",
    githubUrl: "https://github.com/Ethan-YoungQ/Invoice-Downloader",
};

const ZHIPU_PLATFORM_URL = "https://bigmodel.cn/pricing";
const EMAIL_DOMAIN_OPTIONS = [
    { value: "qq.com", label: "qq.com" },
    { value: "163.com", label: "163.com" },
];

const DISCLAIMER_SOFTWARE_ITEMS = [
    "本软件用于票据整理、归档与复核辅助处理。",
    "本软件不附带任何真实邮箱凭据或 API Key。",
    "请仅填写并使用你自己的邮箱授权信息与 API Key。",
    "涉及报销、入账或合规判断的重要票据，请务必进行人工复核。",
];

const DISCLAIMER_ITEMS = [
    "本工具用于票据整理与归档辅助，识别结果应结合业务流程进行复核。",
    "对于识别、归档、遗漏、误判及由此产生的后果，工具方不承担责任。",
    "使用者应自行遵守所在公司关于大模型、API 使用和数据安全管理的规定。",
    "涉及报销、入账或合规判断的重要票据，请务必进行人工复核。",
];

const UI_COPY = {
    shell: {
        close: "关闭程序",
        closing: "正在关闭...",
        closeFailed: "关闭程序失败，请稍后重试。",
        minimize: "最小化窗口",
        minimizing: "正在最小化...",
        minimizeFailed: "最小化窗口失败，请稍后重试。",
        windowSubtitle: "桌面工作区",
    },
    navigation: [
        { key: "settings", label: "启动配置", description: "邮箱、API 与日期", icon: "tune", path: "/" },
        { key: "processing", label: "处理中心", description: "进度与日志", icon: "data_thresholding", path: "/processing" },
        { key: "analysis", label: "结果分析", description: "总览与导出", icon: "assessment", path: "/analysis" },
    ],
    pages: {
        settings: {
            eyebrow: "Step 1",
            title: "启动配置",
            footerText: "当前页: 启动配置",
            bootstrapTitle: "启动配置",
            bootstrapDescription: "正在初始化本地设置。",
            bootstrapMessage: "正在连接桌面接口并加载本地设置，请稍候。",
            errorDescription: "本地设置初始化失败。",
            controlledNotice: "受控复跑已锁定输出目录与日期窗口。",
        },
        processing: {
            eyebrow: "Step 2",
            title: "处理中心",
            footerText: "当前页: 处理中心",
            closeHint: "关闭程序会直接结束当前窗口与任务进程，请仅在确认后执行。",
            statusWaiting: "等待任务状态",
            currentOperation: "当前操作",
            progressLabel: "完成度",
            liveRefresh: "进度与日志保持实时刷新。",
            stopPending: "已收到安全停止指令。",
            stopDetail: "系统将在当前文件处理完成后安全停止。",
            processingDetail: "邮件抓取、票据恢复与结构化识别按既有后端流程执行。",
            stopNotice: "已收到安全停止指令，当前邮件或当前文件处理完成后将结束任务。",
        },
        analysis: {
            eyebrow: "Step 3",
            title: "结果分析",
            footerText: "当前页: 结果分析",
            statusSummary: "运行结果",
            resultTitle: "本次处理完成",
            reviewTitle: "待人工复核",
            reviewEmpty: "当前没有待人工复核记录。",
            reviewReady: "请优先前往待人工复核文件夹处理这些记录。",
            reviewIdle: "结果明细仍可导出查看，输出目录会保留本轮成功归档结果。",
        },
    },
};

function joinClasses(...values) {
    return values.filter(Boolean).join(" ");
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function openExternalUrl(url) {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
}

async function waitForApi() {
    for (let attempt = 0; attempt < 120; attempt += 1) {
        if (window.pywebview && window.pywebview.api) {
            return window.pywebview.api;
        }
        await sleep(100);
    }
    throw new Error("桌面接口尚未就绪，请稍后重试。");
}

async function waitForApiMethod(method) {
    const api = await waitForApi();
    for (let attempt = 0; attempt < 120; attempt += 1) {
        if (typeof api[method] === "function") {
            return api[method].bind(api);
        }
        await sleep(100);
    }
    throw new Error(`缺少后端接口: ${method}`);
}

async function callApi(method, ...args) {
    const callTarget = await waitForApiMethod(method);
    return callTarget(...args);
}

function validateEmail(email) {
    const value = String(email || "").trim();
    if (!value) {
        return "请输入邮箱地址。";
    }
    if (!/@(qq|163)\.com$/i.test(value)) {
        return "当前支持 QQ 邮箱和 163 邮箱。";
    }
    return "";
}

/* COMPANY_MAP removed — company is now free-text input */

function validateDateRange(dateFrom, dateTo) {
    const pattern = /^\d{4}-\d{2}-\d{2}$/;
    if (!pattern.test(String(dateFrom || ""))) {
        return "开始日期格式必须为 YYYY-MM-DD。";
    }
    if (!pattern.test(String(dateTo || ""))) {
        return "结束日期格式必须为 YYYY-MM-DD。";
    }
    if (dateFrom > dateTo) {
        return "开始日期不能晚于结束日期。";
    }
    return "";
}

function maskEmail(email) {
    const value = String(email || "");
    const [name, domain] = value.split("@");
    if (!name || !domain) {
        return value || "--";
    }
    if (name.length <= 2) {
        return `${name[0] || ""}***@${domain}`;
    }
    return `${name.slice(0, 2)}***@${domain}`;
}

function safeText(value, fallback = "--") {
    return value === undefined || value === null || value === "" ? fallback : String(value);
}

function preferNonEmpty(...values) {
    for (const value of values) {
        if (value !== undefined && value !== null && String(value).trim() !== "") {
            return value;
        }
    }
    return "";
}

function fileNameFromPath(path, fallback = "未命名文件") {
    const value = String(path || "");
    if (!value) {
        return fallback;
    }
    const parts = value.split(/[/\\]/);
    return parts[parts.length - 1] || fallback;
}

function parentFolder(path) {
    const value = String(path || "");
    if (!value) {
        return "";
    }
    return value.replace(/[/\\][^/\\]+$/, "");
}

const SESSION_SETTINGS_KEY = "invoiceflow.session.settings";
const SESSION_RUN_SETTINGS_KEY = "invoiceflow.session.runSettings";
const CONTROLLED_AUTOSTART_PREFIX = "invoiceflow.controlledAutostart";

function readSessionValue(key) {
    try {
        const raw = window.sessionStorage.getItem(key);
        return raw ? JSON.parse(raw) : {};
    } catch (error) {
        return {};
    }
}

function writeSessionValue(key, value) {
    try {
        window.sessionStorage.setItem(key, JSON.stringify(value || {}));
    } catch (error) {
        console.warn("Failed to write session state", error);
    }
}

function hasExplicitQaRunContext(runContext) {
    return !!(runContext && runContext.explicit_run_context && runContext.controlled_run);
}

function shouldAutostartControlledRun(runContext) {
    return !!(hasExplicitQaRunContext(runContext) && runContext.autostart_enabled);
}

function controlledAutostartStateKey(runContext) {
    const token = safeText((runContext && runContext.autostart_token) || "", "").trim();
    const runId = safeText((runContext && runContext.run_id) || "", "").trim();
    return [CONTROLLED_AUTOSTART_PREFIX, token || runId || "default"].join(".");
}

function markControlledAutostartConsumed(runContext) {
    writeSessionValue(controlledAutostartStateKey(runContext), {
        consumed: true,
        consumed_at: new Date().toISOString(),
    });
}

function isControlledAutostartConsumed(runContext) {
    const payload = readSessionValue(controlledAutostartStateKey(runContext));
    return !!payload.consumed;
}

function buildPersistPayload(settings, runSettings, runContext) {
    const payload = {
        ...DEFAULT_SETTINGS,
        ...DEFAULT_RUN_SETTINGS,
        ...(runSettings || {}),
        ...(settings || {}),
    };

    if (hasExplicitQaRunContext(runContext)) {
        if (runContext.locked_output_path) {
            payload.save_path = runContext.locked_output_path;
        }
        if (runContext.locked_date_from) {
            payload.date_from = runContext.locked_date_from;
        }
        if (runContext.locked_date_to) {
            payload.date_to = runContext.locked_date_to;
        }
    }

    return payload;
}

async function loadShellState() {
    const [settingsRes, runContextRes] = await Promise.all([
        callApi("load_user_settings"),
        callApi("get_run_context").catch(() => ({})),
    ]);

    const storedPayload = Object.assign({}, DEFAULT_SETTINGS, DEFAULT_RUN_SETTINGS, (settingsRes && settingsRes.settings) || {});
    const sessionSettings = readSessionValue(SESSION_SETTINGS_KEY);
    const sessionRunSettings = readSessionValue(SESSION_RUN_SETTINGS_KEY);
    const runContext = runContextRes || {};

    const settings = {
        email: preferNonEmpty(sessionSettings.email, storedPayload.email),
        // Sensitive values must not be revived from WebView session state.
        auth_code: preferNonEmpty(storedPayload.auth_code),
        api_key: preferNonEmpty(storedPayload.api_key),
        save_path: preferNonEmpty(sessionSettings.save_path, storedPayload.save_path),
        company: preferNonEmpty(sessionSettings.company, storedPayload.company),
        remember_settings: sessionSettings.remember_settings === undefined
            ? storedPayload.remember_settings !== false
            : sessionSettings.remember_settings !== false,
    };

    const runSettings = {
        date_from: preferNonEmpty(sessionRunSettings.date_from, storedPayload.date_from, DEFAULT_RUN_SETTINGS.date_from),
        date_to: preferNonEmpty(sessionRunSettings.date_to, storedPayload.date_to, DEFAULT_RUN_SETTINGS.date_to),
        quick_range: preferNonEmpty(sessionRunSettings.quick_range, storedPayload.quick_range, "last_30_days"),
    };

    if (hasExplicitQaRunContext(runContext)) {
        if (runContext.locked_email) {
            settings.email = runContext.locked_email;
        }
        if (runContext.locked_output_path) {
            settings.save_path = runContext.locked_output_path;
        }
        if (runContext.locked_date_from) {
            runSettings.date_from = runContext.locked_date_from;
        }
        if (runContext.locked_date_to) {
            runSettings.date_to = runContext.locked_date_to;
        }
    }

    return { settings, runSettings, runContext };
}

async function persistUserSettings(settings, runSettings, runContext) {
    const payload = buildPersistPayload(settings, runSettings, runContext);
    writeSessionValue(SESSION_SETTINGS_KEY, {
        email: payload.email || "",
        save_path: payload.save_path || "",
        company: payload.company || "",
        remember_settings: payload.remember_settings !== false,
    });
    writeSessionValue(SESSION_RUN_SETTINGS_KEY, {
        date_from: payload.date_from || "",
        date_to: payload.date_to || "",
        quick_range: payload.quick_range || DEFAULT_RUN_SETTINGS.quick_range,
    });

    if (payload.remember_settings === false) {
        return callApi("save_user_settings", { remember_settings: false });
    }
    return callApi("save_user_settings", payload);
}

function toneFromAsyncStatus(status) {
    if (status === "success") return "success";
    if (status === "error") return "error";
    if (status === "testing") return "info";
    return "info";
}

function splitEmailAddress(email) {
    const value = String(email || "").trim();
    const [username = "", rawDomain = ""] = value.split("@");
    const domain = rawDomain === "163.com" ? "163.com" : "qq.com";
    return { username, domain };
}

function resolveGroupTone(groupKey) {
    if (groupKey === "manual_review") return "group-chip--warning";
    if (groupKey === "retained_record") return "group-chip--info";
    if (groupKey === "processing_error") return "group-chip--error";
    return "";
}

function resolveLogToneClass(log) {
    const sample = `${String(log && log.color || "")} ${String(log && log.type || "")}`.toLowerCase();
    if (sample.includes("error") || sample.includes("red")) return "terminal-kind terminal-kind--error";
    if (sample.includes("warning") || sample.includes("warn") || sample.includes("amber") || sample.includes("orange")) return "terminal-kind terminal-kind--warning";
    if (sample.includes("success") || sample.includes("green") || sample.includes("emerald")) return "terminal-kind terminal-kind--success";
    if (sample.includes("info") || sample.includes("blue") || sample.includes("cyan")) return "terminal-kind terminal-kind--info";
    return "terminal-kind";
}

function NoticeBox({ tone = "info", className = "", children }) {
    return <div className={joinClasses("notice", `notice--${tone}`, className)}>{children}</div>;
}

function InlineStatus({ tone = "info", children }) {
    const icon = tone === "success" ? "task_alt" : tone === "error" ? "error" : tone === "warning" ? "warning" : "info";
    return (
        <div className={joinClasses("inline-status", `inline-status--${tone}`)}>
            <span className="material-symbols-outlined inline-status__icon">{icon}</span>
            <span className="inline-status__text">{children}</span>
        </div>
    );
}

function StatusPill({ tone = "neutral", icon, children }) {
    return (
        <span className={joinClasses("status-pill", tone !== "neutral" && `status-pill--${tone}`)}>
            {icon && <span className="material-symbols-outlined" style={{ fontSize: 16 }}>{icon}</span>}
            <span>{children}</span>
        </span>
    );
}

function SectionHeader({ icon, title, indicator }) {
    return (
        <div className="section-header">
            <div className="section-title">
                <span className="section-title__icon material-symbols-outlined">{icon}</span>
                <span>{title}</span>
            </div>
            {indicator ? <div className="u-text-muted" style={{ fontSize: 12 }}>{indicator}</div> : null}
        </div>
    );
}

function PageHeader({ eyebrow, title, description, badge }) {
    return (
        <div className="page-header-shell">
            <div>
                {eyebrow ? <p className="page-eyebrow">{eyebrow}</p> : null}
                <h1 className="page-title">{title}</h1>
                {description ? <p className="page-description">{description}</p> : null}
            </div>
            {badge ? <div className="page-header-badge">{badge}</div> : null}
        </div>
    );
}

function PageFooter({ left, right }) {
    return (
        <footer className="footer-bar">
            <div className="footer-slot">{left}</div>
            <div className="footer-slot footer-slot--right">{right}</div>
        </footer>
    );
}

function DarkSelect({ value, options, onChange, disabled = false, ariaLabel }) {
    const [open, setOpen] = useState(false);
    const rootRef = useRef(null);
    const activeOption = options.find((option) => option.value === value) || options[0] || { value: "", label: "" };

    useEffect(() => {
        if (!open) return undefined;

        function handlePointerDown(event) {
            if (rootRef.current && !rootRef.current.contains(event.target)) {
                setOpen(false);
            }
        }

        function handleKeyDown(event) {
            if (event.key === "Escape") {
                setOpen(false);
            }
        }

        window.addEventListener("mousedown", handlePointerDown);
        window.addEventListener("keydown", handleKeyDown);
        return () => {
            window.removeEventListener("mousedown", handlePointerDown);
            window.removeEventListener("keydown", handleKeyDown);
        };
    }, [open]);

    function handleSelect(nextValue) {
        onChange(nextValue);
        setOpen(false);
    }

    return (
        <div ref={rootRef} className={joinClasses("field-shell", "field-shell--dropdown", disabled && "field-shell--readonly", open && "field-shell--dropdown-open")}>
            <button
                type="button"
                className="field-dropdown-trigger"
                aria-haspopup="listbox"
                aria-expanded={open}
                aria-label={ariaLabel}
                onClick={() => !disabled && setOpen((current) => !current)}
                disabled={disabled}
            >
                <span className="field-dropdown-trigger__label">{activeOption.label}</span>
                <span className="material-symbols-outlined field-dropdown-trigger__icon">{open ? "expand_less" : "expand_more"}</span>
            </button>
            {open ? (
                <div className="field-dropdown-menu" role="listbox" aria-label={ariaLabel}>
                    {options.map((option) => (
                        <button
                            key={option.value}
                            type="button"
                            className={joinClasses("field-dropdown-option", option.value === activeOption.value && "is-active")}
                            onClick={() => handleSelect(option.value)}
                        >
                            <span>{option.label}</span>
                            {option.value === activeOption.value ? <span className="material-symbols-outlined">check</span> : null}
                        </button>
                    ))}
                </div>
            ) : null}
        </div>
    );
}

function DateField({ label, value, onChange, disabled = false }) {
    const inputRef = useRef(null);

    function openPicker() {
        if (disabled || !inputRef.current) return;
        if (typeof inputRef.current.showPicker === "function") {
            try {
                inputRef.current.showPicker();
                return;
            } catch (error) {
                // Fall back to focus/click on runtimes without showPicker support.
            }
        }
        inputRef.current.focus();
        inputRef.current.click();
    }

    return (
        <div className="field-block" style={{ flex: 1 }}>
            <label className="field-label">{label}</label>
            <div className={joinClasses("field-shell", "field-shell--date", disabled && "field-shell--readonly")}>
                <input
                    ref={inputRef}
                    type="date"
                    className="field-input field-input--date"
                    value={value}
                    onChange={(event) => onChange(event.target.value)}
                    onClick={openPicker}
                    disabled={disabled}
                />
                <button type="button" className="field-shell-button" onClick={openPicker} disabled={disabled} aria-label={`${label}日历`}>
                    <span className="material-symbols-outlined">calendar_month</span>
                </button>
            </div>
        </div>
    );
}

function AppWindowChrome({ active }) {
    const [closing, setClosing] = useState(false);
    const [minimizing, setMinimizing] = useState(false);
    const activeItem = UI_COPY.navigation.find((item) => item.key === active);

    async function handleMinimize() {
        if (minimizing) return;
        setMinimizing(true);
        try {
            const result = await callApi("minimize_window");
            if (!result || !result.success) {
                throw new Error((result && result.message) || UI_COPY.shell.minimizeFailed);
            }
        } catch (error) {
            setMinimizing(false);
            window.alert(error.message || UI_COPY.shell.minimizeFailed);
        }
    }

    async function handleClose() {
        if (closing) return;
        setClosing(true);
        try {
            const result = await callApi("close_window");
            if (!result || !result.success) {
                throw new Error((result && result.message) || UI_COPY.shell.closeFailed);
            }
        } catch (error) {
            setClosing(false);
            window.alert(error.message || UI_COPY.shell.closeFailed);
        }
    }

    return (
        <div className="window-chrome">
            <div className="window-drag-region">
                <div className="window-title-stack">
                    <span className="window-title">{APP_BRAND.name}</span>
                    <span className="window-subtitle">{(activeItem && activeItem.label) || UI_COPY.shell.windowSubtitle}</span>
                </div>
            </div>
            <div className="window-controls">
                <button
                    type="button"
                    className="window-traffic-button window-traffic-button--minimize"
                    onClick={handleMinimize}
                    disabled={minimizing || closing}
                    aria-label={minimizing ? UI_COPY.shell.minimizing : UI_COPY.shell.minimize}
                    title={minimizing ? UI_COPY.shell.minimizing : UI_COPY.shell.minimize}
                >
                    <span className="window-traffic-button__glyph"></span>
                </button>
                <button
                    type="button"
                    className="window-traffic-button window-traffic-button--close"
                    onClick={handleClose}
                    disabled={closing}
                    aria-label={closing ? UI_COPY.shell.closing : UI_COPY.shell.close}
                    title={closing ? UI_COPY.shell.closing : UI_COPY.shell.close}
                >
                    <span className="window-traffic-button__glyph"></span>
                </button>
            </div>
        </div>
    );
}

function AppShell({ active, onOpenDisclaimer, children, footerLeft, footerRight, contentClassName = "", contentScrollable = true }) {
    return (
        <div className="app-shell">
            <Sidebar active={active} onOpenDisclaimer={onOpenDisclaimer} />
            <main className="app-main">
                <AppWindowChrome active={active} />
                <div className={joinClasses("page-scroll", !contentScrollable && "page-scroll--locked", contentClassName)}>{children}</div>
                <PageFooter left={footerLeft} right={footerRight} />
            </main>
        </div>
    );
}

function BootstrapStatePage({ active, onOpenDisclaimer, eyebrow, title, description, tone, message, footerText }) {
    return (
        <AppShell active={active} onOpenDisclaimer={onOpenDisclaimer} footerRight={<p className="footer-meta">{footerText}</p>}>
            <div className="page-wrap page-wrap--narrow">
                <PageHeader eyebrow={eyebrow} title={title} description={description} />
                <NoticeBox tone={tone}>{message}</NoticeBox>
            </div>
        </AppShell>
    );
}

function SummaryField({ span = 6, label, value, helper, icon, tone = "", mono = false, truncate = false }) {
    return (
        <div className={joinClasses("summary-item", tone)} style={{ gridColumn: `span ${span} / span ${span}` }}>
            <div className="summary-item__icon"><span className="material-symbols-outlined">{icon}</span></div>
            <div className="summary-item__copy">
                <p className="summary-item__label">{label}</p>
                <p className={joinClasses("summary-item__value", mono && "u-mono", truncate && "u-truncate")}>{value}</p>
                {helper ? <p className="summary-item__helper">{helper}</p> : null}
            </div>
        </div>
    );
}

function DisclaimerDialog({ open, onClose }) {
    useEffect(() => {
        if (!open) return undefined;
        function handleKeydown(event) {
            if (event.key === "Escape") onClose();
        }
        window.addEventListener("keydown", handleKeydown);
        return () => window.removeEventListener("keydown", handleKeydown);
    }, [open, onClose]);

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-card" onClick={(event) => event.stopPropagation()}>
                <div className="modal-head">
                    <div>
                        <p className="page-eyebrow" style={{ marginBottom: 10 }}>免责声明</p>
                        <h2 className="page-title" style={{ fontSize: 28 }}>使用前请确认以下事项</h2>
                    </div>
                    <button type="button" className="btn btn--ghost btn--sm" onClick={onClose}>
                        <span className="material-symbols-outlined">close</span>
                    </button>
                </div>
                <div className="modal-body">
                    <section className="modal-section">
                        <p className="modal-label">软件说明</p>
                        <ul className="modal-list">
                            {DISCLAIMER_SOFTWARE_ITEMS.map((item) => <li key={item}>{item}</li>)}
                        </ul>
                    </section>
                    <section className="modal-section">
                        <p className="modal-label">风险提示</p>
                        <ul className="modal-list">
                            {DISCLAIMER_ITEMS.map((item) => <li key={item}>{item}</li>)}
                        </ul>
                    </section>
                </div>
                <div className="modal-footer">
                    <button type="button" className="btn btn--primary" onClick={onClose}>我已知晓</button>
                </div>
            </div>
        </div>
    );
}

function Sidebar({ active, onOpenDisclaimer }) {
    const navigate = useNavigate();
    const githubUrl = APP_VISIBLE_COPY.githubUrl;

    return (
        <aside className="app-sidebar">
            <div className="sidebar-top">
                <div className="sidebar-logo">IF</div>
                <div>
                    <p className="sidebar-title">{APP_BRAND.name}</p>
                    <p className="sidebar-subtitle">{APP_VISIBLE_COPY.topSubtitle}</p>
                </div>
            </div>

            <nav className="sidebar-nav">
                {UI_COPY.navigation.map((item) => (
                    <button
                        key={item.key}
                        type="button"
                        className={joinClasses("sidebar-nav__item", active === item.key && "is-active")}
                        onClick={() => navigate(item.path)}
                    >
                        <span className="sidebar-nav__item-icon material-symbols-outlined">{item.icon}</span>
                        <span className="sidebar-nav__item-copy">
                            <strong>{item.label}</strong>
                            <span>{item.description}</span>
                        </span>
                    </button>
                ))}
            </nav>

            <div className="sidebar-footer">
                <button type="button" className="sidebar-foot-button" onClick={() => navigate("/")}>
                    <span className="material-symbols-outlined">restart_alt</span>
                    <span className="sidebar-foot-button__label">回到首页</span>
                </button>
                <button type="button" className="sidebar-foot-button" onClick={onOpenDisclaimer}>
                    <span className="material-symbols-outlined">balance</span>
                    <span className="sidebar-foot-button__label">免责声明</span>
                </button>
                <div className="sidebar-link-row">
                    <button type="button" className="sidebar-foot-chip sidebar-foot-chip--single" onClick={() => openExternalUrl(githubUrl)}>
                        <span className="material-symbols-outlined">code</span>
                        <span>{APP_VISIBLE_COPY.githubLabel}</span>
                    </button>
                </div>
                <div className="sidebar-brand">
                    <span className="sidebar-brand__dot"></span>
                    <div>
                        <p className="sidebar-brand__name">{APP_VISIBLE_COPY.footerVersion}</p>
                        <p className="sidebar-brand__sub">{APP_VISIBLE_COPY.footerStamp}</p>
                    </div>
                </div>
            </div>
        </aside>
    );
}

function SettingsPage({ onOpenDisclaimer }) {
    const navigate = useNavigate();
    const [settings, setSettings] = useState(DEFAULT_SETTINGS);
    const [runSettings, setRunSettings] = useState(DEFAULT_RUN_SETTINGS);
    const [runContext, setRunContext] = useState({ controlled_run: false, explicit_run_context: false });
    const [showAuthCode, setShowAuthCode] = useState(false);
    const [showApiKey, setShowApiKey] = useState(false);
    const [pageError, setPageError] = useState("");
    const [emailStatus, setEmailStatus] = useState({ status: "idle", message: "" });
    const [apiStatus, setApiStatus] = useState({ status: "idle", message: "" });
    const [bootstrapState, setBootstrapState] = useState("bootstrapping");
    const [bootstrapError, setBootstrapError] = useState("");
    const [starting, setStarting] = useState(false);
    const saveTimerRef = useRef(null);
    const autostartTimerRef = useRef(null);
    const autostartTriggeredRef = useRef(false);

    useEffect(() => {
        let active = true;
        (async () => {
            try {
                const state = await loadShellState();
                if (!active) return;
                setSettings(state.settings);
                setRunSettings(state.runSettings);
                setRunContext(state.runContext);
                setBootstrapError("");
                setBootstrapState("bootstrapped");
            } catch (error) {
                if (active) {
                    setBootstrapError(error.message || "初始化设置失败，请重启应用。");
                    setBootstrapState("bootstrap_failed");
                }
            }
        })();
        return () => {
            active = false;
            if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
            if (autostartTimerRef.current) clearTimeout(autostartTimerRef.current);
        };
    }, []);

    useEffect(() => {
        if (bootstrapState !== "bootstrapped") return undefined;
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => {
            persistUserSettings(settings, runSettings, runContext).catch((error) => {
                console.error("Failed to save settings", error);
            });
        }, 400);
        return () => {
            if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        };
    }, [bootstrapState, settings, runSettings, runContext]);

    const controlledRun = hasExplicitQaRunContext(runContext);
    const emailParts = splitEmailAddress(settings.email);
    const dateError = validateDateRange(runSettings.date_from, runSettings.date_to);
    const canStart = !validateEmail(settings.email)
        && !!settings.auth_code
        && !!settings.api_key
        && !!String(settings.company || "").trim()
        && !!String(settings.save_path || "").trim()
        && !dateError;

    useEffect(() => {
        if (bootstrapState !== "bootstrapped" || starting || autostartTriggeredRef.current || !shouldAutostartControlledRun(runContext)) return undefined;
        if (isControlledAutostartConsumed(runContext)) {
            autostartTriggeredRef.current = true;
            return undefined;
        }
        const delayMs = Math.max(0, Number(runContext.autostart_delay_ms || 0));
        autostartTimerRef.current = setTimeout(() => {
            autostartTriggeredRef.current = true;
            markControlledAutostartConsumed(runContext);
            handleStart();
        }, delayMs);
        return () => {
            if (autostartTimerRef.current) clearTimeout(autostartTimerRef.current);
        };
    }, [bootstrapState, runContext, starting, settings, runSettings]);

    function updateSetting(key, value) {
        setSettings((current) => ({ ...current, [key]: value }));
        setPageError("");
        if (key === "email" || key === "auth_code") setEmailStatus({ status: "idle", message: "" });
        if (key === "email" || key === "api_key") setApiStatus({ status: "idle", message: "" });
    }

    function setDateValue(field, value) {
        setRunSettings((current) => ({ ...current, [field]: value, quick_range: "custom" }));
        setPageError("");
    }

    async function handleChooseDirectory() {
        if (controlledRun) return;
        try {
            const result = await callApi("choose_directory");
            if (result && result.success && result.path) updateSetting("save_path", result.path);
        } catch (error) {
            setPageError(error.message || "选择目录失败。");
        }
    }

    async function handleTestEmailAuth() {
        const emailError = validateEmail(settings.email);
        if (emailError) {
            setEmailStatus({ status: "error", message: emailError });
            return;
        }
        if (!settings.auth_code) {
            setEmailStatus({ status: "error", message: "请输入邮箱授权码。" });
            return;
        }
        setEmailStatus({ status: "testing", message: "正在测试邮箱授权码..." });
        try {
            const result = await callApi("test_email_auth", String(settings.email).trim(), settings.auth_code);
            setEmailStatus({ status: result && result.success ? "success" : "error", message: result && result.message ? result.message : "邮箱授权验证失败。" });
        } catch (error) {
            setEmailStatus({ status: "error", message: error.message || "邮箱授权验证失败。" });
        }
    }

    async function handleTestConnection() {
        if (!settings.api_key) {
            setApiStatus({ status: "error", message: "请输入 GLM API Key。" });
            return;
        }
        setApiStatus({ status: "testing", message: "正在测试 API Key..." });
        try {
            const result = await callApi(
                "test_connection",
                String(settings.email || "").trim(),
                String(settings.auth_code || "").trim(),
                settings.api_key
            );
            setApiStatus({ status: result && result.success ? "success" : "error", message: result && result.message ? result.message : "API Key 测试失败。" });
        } catch (error) {
            setApiStatus({ status: "error", message: error.message || "API Key 测试失败。" });
        }
    }

    async function handleStart() {
        const emailError = validateEmail(settings.email);
        if (emailError) return setPageError(emailError);
        if (!settings.auth_code) return setPageError("请输入邮箱授权码。");
        if (!settings.api_key) return setPageError("请输入 GLM API Key。");
        if (!settings.company || !settings.company.trim()) return setPageError("请填写公司名称。");
        if (!settings.save_path) return setPageError("请选择输出目录。");
        if (dateError) return setPageError(dateError);
        setStarting(true);
        try {
            await persistUserSettings(settings, runSettings, runContext);
            const result = await callApi("start_processing", "", settings.save_path, runSettings.date_from, runSettings.date_to, String(settings.email).trim(), settings.auth_code, settings.api_key);
            if (!result || !result.success) {
                setPageError(result && result.message ? result.message : "任务启动失败。");
                return;
            }
            navigate("/processing");
        } catch (error) {
            setPageError(error.message || "任务启动失败。");
        } finally {
            setStarting(false);
        }
    }
    if (bootstrapState === "bootstrapping") {
        return <BootstrapStatePage active="settings" onOpenDisclaimer={onOpenDisclaimer} eyebrow={UI_COPY.pages.settings.eyebrow} title={UI_COPY.pages.settings.bootstrapTitle} description={UI_COPY.pages.settings.bootstrapDescription} tone="info" message={UI_COPY.pages.settings.bootstrapMessage} footerText={UI_COPY.pages.settings.footerText} />;
    }
    if (bootstrapState === "bootstrap_failed") {
        return <BootstrapStatePage active="settings" onOpenDisclaimer={onOpenDisclaimer} eyebrow={UI_COPY.pages.settings.eyebrow} title={UI_COPY.pages.settings.bootstrapTitle} description={UI_COPY.pages.settings.errorDescription} tone="error" message={bootstrapError || "初始化设置失败，请重启应用。"} footerText={UI_COPY.pages.settings.footerText} />;
    }

    return (
        <AppShell
            active="settings"
            onOpenDisclaimer={onOpenDisclaimer}
            footerLeft={
                <label className="toggle-row">
                    <input type="checkbox" checked={settings.remember_settings !== false} onChange={(event) => updateSetting("remember_settings", event.target.checked)} />
                    <span>自动记住当前配置</span>
                </label>
            }
            footerRight={
                <button type="button" className="btn btn--primary" onClick={handleStart} disabled={!canStart || starting}>
                    <span className="material-symbols-outlined">{starting ? "sync" : "play_arrow"}</span>
                    <span>{starting ? "启动中..." : "开始提取"}</span>
                </button>
            }
        >
            <div className="page-wrap page-wrap--settings">
                <PageHeader eyebrow={UI_COPY.pages.settings.eyebrow} title={UI_COPY.pages.settings.title} badge={controlledRun ? <StatusPill tone="info" icon="lock_clock">受控前端复跑</StatusPill> : null} />
                {pageError ? <NoticeBox tone="error">{pageError}</NoticeBox> : null}
                {controlledRun ? <p className="page-inline-note">{UI_COPY.pages.settings.controlledNotice}</p> : null}

                <div className="settings-grid settings-grid--fit">
                    <div className="settings-column">
                        <section className="surface-card">
                            <SectionHeader icon="mail" title="邮箱源配置" indicator={<StatusPill tone="warning" icon="mail">待确认</StatusPill>} />
                            <div className="card-stack card-stack--compact">
                                <div className="field-block">
                                    <label className="field-label">邮箱地址</label>
                                    <div className="field-row">
                                        <div className="field-shell" style={{ flex: 1 }}>
                                            <span className="field-icon material-symbols-outlined">alternate_email</span>
                                            <input type="text" className="field-input" placeholder="邮箱用户名" value={emailParts.username} onChange={(event) => {
                                                const username = event.target.value.replace(/@/g, "");
                                                updateSetting("email", username ? `${username}@${emailParts.domain}` : "");
                                            }} />
                                        </div>
                                        <span className="u-text-muted">@</span>
                                        <div style={{ width: 136 }}>
                                            <DarkSelect
                                                value={emailParts.domain}
                                                options={EMAIL_DOMAIN_OPTIONS}
                                                ariaLabel="邮箱域名"
                                                onChange={(domain) => updateSetting("email", emailParts.username ? `${emailParts.username}@${domain}` : "")}
                                            />
                                        </div>
                                    </div>
                                    <p className="field-help">当前支持 QQ 邮箱与 163 邮箱，域名会决定 IMAP 通道。</p>
                                </div>

                                <div className="field-block">
                                    <label className="field-label">授权码</label>
                                    <div className="field-shell">
                                        <span className="field-icon material-symbols-outlined">key</span>
                                        <input type={showAuthCode ? "text" : "password"} className="field-input u-mono" placeholder="请输入邮箱授权码" value={settings.auth_code} onChange={(event) => updateSetting("auth_code", event.target.value)} />
                                        <button type="button" className="field-mask-toggle" onClick={() => setShowAuthCode((current) => !current)}>
                                            <span className="material-symbols-outlined">{showAuthCode ? "visibility" : "visibility_off"}</span>
                                        </button>
                                    </div>
                                    <div className="footer-cluster">
                                        <button type="button" className="btn btn--secondary btn--sm" onClick={handleTestEmailAuth} disabled={emailStatus.status === "testing"}>
                                            <span className="material-symbols-outlined">{emailStatus.status === "testing" ? "sync" : "verified"}</span>
                                            <span>{emailStatus.status === "testing" ? "测试中..." : "测试邮箱授权码"}</span>
                                        </button>
                                    </div>
                                    {emailStatus.status !== "idle" ? <InlineStatus tone={toneFromAsyncStatus(emailStatus.status)}>{emailStatus.message}</InlineStatus> : null}
                                </div>
                            </div>
                        </section>

                        <section className="surface-card">
                            <SectionHeader icon="folder_open" title="输出目录设置" indicator={controlledRun ? <StatusPill tone="info" icon="lock">已锁定</StatusPill> : "保存原件与结果"} />
                            <div className="card-stack card-stack--compact">
                                <div className={joinClasses("field-shell", "field-shell--readonly", !settings.save_path && "field-shell--error")}>
                                    <span className="field-icon material-symbols-outlined">folder</span>
                                    <input type="text" readOnly className="field-input u-mono" value={settings.save_path} placeholder="请选择本地输出目录" title={settings.save_path} />
                                </div>
                                <div className="footer-cluster">
                                    <button type="button" className="btn btn--secondary btn--sm" onClick={handleChooseDirectory} disabled={controlledRun}>
                                        <span className="material-symbols-outlined">folder_open</span>
                                        <span>{controlledRun ? "路径已锁定" : "浏览目录"}</span>
                                    </button>
                                </div>
                                <p className="field-help">{controlledRun ? "当前为受控前端复跑，本轮输出目录由诊断上下文锁定。" : "系统会在此目录下继续按发票类型落盘，并生成待人工复核目录。"}</p>
                            </div>
                        </section>
                    </div>

                    <div className="settings-column">
                        <section className="surface-card">
                            <SectionHeader icon="psychology" title="智能处理引擎" indicator={<StatusPill tone="success" icon="auto_awesome">GLM</StatusPill>} />
                            <div className="card-stack card-stack--compact">
                                <div className="field-block">
                                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                                        <label className="field-label">GLM API Key</label>
                                        <a className="field-inline-action" href={ZHIPU_PLATFORM_URL} target="_blank" rel="noreferrer"><span className="material-symbols-outlined" style={{ fontSize: 14 }}>open_in_new</span><span>购买 API / 获取额度</span></a>
                                    </div>
                                    <div className="field-row">
                                        <div className="field-shell" style={{ flex: 1 }}>
                                            <span className="field-icon material-symbols-outlined">vpn_key</span>
                                            <input type={showApiKey ? "text" : "password"} className="field-input u-mono" placeholder="sk-..." value={settings.api_key} onChange={(event) => updateSetting("api_key", event.target.value)} />
                                            <button type="button" className="field-mask-toggle" onClick={() => setShowApiKey((current) => !current)}><span className="material-symbols-outlined">{showApiKey ? "visibility" : "visibility_off"}</span></button>
                                        </div>
                                        <button type="button" className="btn btn--secondary btn--sm" onClick={handleTestConnection} disabled={apiStatus.status === "testing"}>
                                            <span className="material-symbols-outlined">{apiStatus.status === "testing" ? "sync" : "link"}</span>
                                            <span>{apiStatus.status === "testing" ? "测试中..." : "测试 API Key"}</span>
                                        </button>
                                    </div>
                                    {apiStatus.status === "idle" ? <p className="field-help">额度不足时会直接提示，后续处理页也会同步显示 quota 相关警告。</p> : null}
                                    {apiStatus.status !== "idle" ? <InlineStatus tone={toneFromAsyncStatus(apiStatus.status)}>{apiStatus.message}</InlineStatus> : null}
                                </div>
                            </div>
                        </section>

                        <section className="surface-card surface-card--compact settings-date-card">
                            <SectionHeader icon="calendar_month" title="提取时间范围" indicator={controlledRun ? <StatusPill tone="info" icon="lock">已锁定</StatusPill> : null} />
                            <div className="card-stack card-stack--compact">
                                <div className="field-row field-row--dates settings-date-row">
                                    <DateField label="开始日期" value={runSettings.date_from} onChange={(value) => setDateValue("date_from", value)} disabled={controlledRun} />
                                    <span className="field-separator">—</span>
                                    <DateField label="结束日期" value={runSettings.date_to} onChange={(value) => setDateValue("date_to", value)} disabled={controlledRun} />
                                </div>
                                {controlledRun ? <p className="field-help field-help--subtle">当前为受控复跑，日期范围已按上下文锁定。</p> : <p className="field-help field-help--subtle">确认起止日期后可直接开始提取，本页不再进入独立确认流程。</p>}
                            </div>
                        </section>

                        <section className="surface-card">
                            <SectionHeader icon="business" title="公司配置" indicator={<StatusPill tone={settings.company && settings.company.trim() ? "success" : "warning"} icon="apartment">购买方校验</StatusPill>} />
                            <div className="card-stack card-stack--compact">
                                <div className="field-block">
                                    <label className="field-label">公司 <span className="field-required">*</span></label>
                                    <div className={joinClasses("field-shell", !settings.company || !settings.company.trim() ? "field-shell--error" : "") }>
                                        <span className="field-icon material-symbols-outlined">domain</span>
                                        <input type="text" className="field-input" placeholder="填写用于匹配购买方的公司名称" value={settings.company || ""} onChange={(event) => updateSetting("company", event.target.value)} />
                                    </div>
                                    {!settings.company || !settings.company.trim()
                                        ? <p className="field-help field-help--subtle">填写公司名称后按购买方字段匹配。</p>
                                        : <p className="field-help field-help--subtle">按购买方包含“{settings.company.trim()}”进行匹配；明确不匹配的票据会单独进入“非目标公司发票”。</p>}
                                </div>
                            </div>
                        </section>
                    </div>
                </div>
            </div>
        </AppShell>
    );
}

function ProcessingPage({ onOpenDisclaimer }) {
    const navigate = useNavigate();
    const [progressState, setProgressState] = useState(DEFAULT_PROGRESS);
    const redirectRef = useRef(false);
    const terminalBodyRef = useRef(null);

    useEffect(() => {
        let active = true;
        let timer = null;
        const poll = async () => {
            try {
                const data = await callApi("get_progress");
                if (!active || !data) return;
                setProgressState({ ...DEFAULT_PROGRESS, ...data, stats: Object.assign({}, DEFAULT_PROGRESS.stats, data.stats || {}) });
                if (!redirectRef.current && ["completed", "failed"].includes(data.run_state) && !data.is_running) {
                    redirectRef.current = true;
                    setTimeout(() => navigate("/analysis"), 1200);
                }
            } catch (error) {
                if (active) setProgressState((current) => ({ ...current, last_error: error.message || "获取进度失败。" }));
            }
        };
        poll();
        timer = setInterval(poll, 1000);
        return () => {
            active = false;
            if (timer) clearInterval(timer);
        };
    }, [navigate]);

    async function handleStop() {
        if (!progressState.can_stop) return;
        try {
            const result = await callApi("stop_processing");
            if (!result || !result.success) window.alert(result && result.message ? result.message : "停止指令发送失败。");
        } catch (error) {
            window.alert(error.message || "停止指令发送失败。");
        }
    }

    const stats = progressState.stats || DEFAULT_PROGRESS.stats;
    const logs = progressState.logs || [];
    const statusTone = progressState.run_state === "failed" ? "error" : progressState.run_state === "completed" ? "success" : progressState.is_running ? "info" : "neutral";
    const statusLabel = progressState.run_state === "failed" ? "处理失败" : progressState.run_state === "completed" ? "处理完成" : progressState.is_running ? "实时连接已建立" : UI_COPY.pages.processing.statusWaiting;

    useEffect(() => {
        if (!terminalBodyRef.current) return;
        terminalBodyRef.current.scrollTop = terminalBodyRef.current.scrollHeight;
    }, [logs, progressState.is_running, progressState.stop_requested]);

    return (
        <AppShell
            active="processing"
            onOpenDisclaimer={onOpenDisclaimer}
            contentScrollable={false}
            footerLeft={<p className="footer-meta">{UI_COPY.pages.processing.closeHint}</p>}
            footerRight={<StatusPill tone={statusTone} icon={progressState.is_running ? "radar" : "schedule"}>{statusLabel}</StatusPill>}
        >
            <div className="page-wrap page-wrap--processing">
                <PageHeader eyebrow={UI_COPY.pages.processing.eyebrow} title={UI_COPY.pages.processing.title} />

                <section className="surface-card surface-card--hero">
                    <div className="progress-hero">
                        <div className="progress-main">
                            <div>
                                <p className="progress-kicker">{UI_COPY.pages.processing.currentOperation}</p>
                                <h2 className="progress-title">{progressState.status_text}</h2>
                                <p className="progress-meta">{progressState.stop_requested ? UI_COPY.pages.processing.stopDetail : UI_COPY.pages.processing.processingDetail}</p>
                            </div>
                            <div className="progress-actions">
                                <div className="progress-value"><strong className="u-tabular">{progressState.progress}%</strong><span>{UI_COPY.pages.processing.progressLabel}</span></div>
                                <button type="button" className="btn btn--danger" onClick={handleStop} disabled={!progressState.can_stop}><span className="material-symbols-outlined">{progressState.stop_requested ? "sync" : "close"}</span><span>{progressState.stop_requested ? "正在停止..." : "停止运行"}</span></button>
                            </div>
                        </div>
                        <div className="progress-track"><div className="progress-fill" style={{ width: `${progressState.progress}%` }}></div></div>
                        <div className="progress-caption"><span className="material-symbols-outlined">sync</span><span>{progressState.stop_requested ? UI_COPY.pages.processing.stopPending : UI_COPY.pages.processing.liveRefresh}</span></div>
                        {progressState.last_error && progressState.run_state === "failed" ? <NoticeBox tone="error">{progressState.last_error}</NoticeBox> : null}
                        {progressState.quota_exhausted && progressState.quota_message ? <NoticeBox tone="warning">{progressState.quota_message}</NoticeBox> : null}
                        {progressState.stop_requested && progressState.run_state !== "failed" ? <NoticeBox tone="warning">{UI_COPY.pages.processing.stopNotice}</NoticeBox> : null}
                    </div>
                </section>

                <div className="metrics-grid">
                    <div className="metric-card metric-card--blue"><div className="metric-card__icon"><span className="material-symbols-outlined">mail</span></div><div className="metric-card__copy"><p className="metric-card__label">已扫描邮件</p><p className="metric-card__value u-tabular">{stats.emails || 0}</p></div></div>
                    <div className="metric-card metric-card--green"><div className="metric-card__icon"><span className="material-symbols-outlined">description</span></div><div className="metric-card__copy"><p className="metric-card__label">已识别发票</p><p className="metric-card__value u-tabular">{stats.invoices || 0}</p></div></div>
                    <div className="metric-card metric-card--amber"><div className="metric-card__icon"><span className="material-symbols-outlined">warning</span></div><div className="metric-card__copy"><p className="metric-card__label">异常处理</p><p className="metric-card__value u-tabular">{stats.errors || 0}</p></div></div>
                </div>

                <section className="surface-card surface-card--terminal processing-terminal" style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
                    <div className="terminal-head"><div className="terminal-title"><span className="material-symbols-outlined">terminal</span><span>实时执行日志</span></div><div className="terminal-dots"><span className="terminal-dot"></span><span className="terminal-dot"></span><span className="terminal-dot"></span></div></div>
                    <div ref={terminalBodyRef} className="terminal-body">
                        <p className="terminal-intro">-- 发票助手引擎已连接 --<br />-- 进度与日志实时刷新中 --</p>
                        {logs.map((log, index) => <div key={`${log.time || "log"}-${index}`} className="terminal-line"><span className="terminal-time">{safeText(log.time, "[实时]")}</span><span className={resolveLogToneClass(log)}>{safeText(log.type, "LOG")}</span><span className="terminal-text">{safeText(log.msg, "-")}</span></div>)}
                        {progressState.is_running ? <div className="terminal-line" style={{ marginTop: 4 }}><span className="terminal-time">[实时]</span><span className="terminal-kind terminal-kind--info">状态</span><span className="terminal-text">{progressState.stop_requested ? "正在安全停止..." : "任务持续执行中..."}<span className="terminal-cursor"></span></span></div> : null}
                    </div>
                </section>
            </div>
        </AppShell>
    );
}

function fileExtension(path) {
    const value = String(path || "").toLowerCase();
    if (!value) return "";
    if (value.endsWith(".url.txt")) return ".url.txt";
    const match = value.match(/(\.[^./\\]+)$/);
    return match ? match[1] : "";
}

function describeResultFile(path) {
    const extension = fileExtension(path);
    if ([".pdf", ".jpg", ".jpeg", ".png", ".ofd", ".xml"].includes(extension)) {
        return { extension, fileKindLabel: extension === ".xml" ? "XML 原件" : extension === ".ofd" ? "OFD 原件" : [".jpg", ".jpeg", ".png"].includes(extension) ? "图片原件" : "PDF 原件" };
    }
    if ([".txt", ".url.txt", ".log", ".json"].includes(extension)) {
        return { extension, fileKindLabel: extension === ".url.txt" ? "链接记录" : "记录文件" };
    }
    return { extension, fileKindLabel: "文件" };
}

function normalizeSuccessInvoices(items) {
    return (items || []).map((item, index) => {
        const path = item.path || "";
        const fileMeta = describeResultFile(path);
        return {
            key: path || `${item.date || "row"}-${index}`,
            date: safeText(item.date),
            amount: safeText(item.amount),
            merchant: safeText(item.merchant || item.vendor),
            archiveType: String(item.category || "").trim() || safeText(fileMeta.fileKindLabel),
            path,
            fileName: fileNameFromPath(path),
        };
    });
}

function normalizeGroupedErrors(groups) {
    return (groups || []).map((group, groupIndex) => ({
        key: group.key || `group-${groupIndex}`,
        label: group.key === "retained_record"
            ? "暂存记录"
            : group.key === "manual_review"
                ? "待人工复核"
                : group.key === "processing_error"
                    ? "真实异常"
                    : group.label || "待处理记录",
        count: Number(group.count || 0),
        items: (group.items || []).map((item, itemIndex) => {
            const path = item.path || "";
            return { rowKey: path || `${group.key || "group"}-${itemIndex}`, date: safeText(item.date), reason: safeText(item.reason, group.label || "待处理"), status: safeText(item.status, "待处理"), merchant: safeText(item.merchant), path, fileName: fileNameFromPath(path) };
        }),
    }));
}

function buildResultStatusText(successCount, manualCheckCount, retentionCount, processingErrorCount) {
    const pendingCount = manualCheckCount + retentionCount + processingErrorCount;
    if (successCount > 0 && pendingCount === 0) return "主要结果已经整理完成，本轮没有需要额外关注的记录。";
    if (successCount > 0 && pendingCount > 0) {
        if (manualCheckCount > 0 && retentionCount > 0 && processingErrorCount > 0) {
            return `已整理 ${successCount} 条成功记录；请先处理 ${manualCheckCount} 条待人工复核记录，另有 ${retentionCount} 条暂存记录与 ${processingErrorCount} 条真实异常需要查看。`;
        }
        if (manualCheckCount > 0 && retentionCount > 0) {
            return `已整理 ${successCount} 条成功记录；请优先查看 ${manualCheckCount} 条待人工复核记录，另有 ${retentionCount} 条暂存记录可按需导出查看。`;
        }
        if (manualCheckCount > 0 && processingErrorCount > 0) {
            return `已整理 ${successCount} 条成功记录；当前需要关注 ${manualCheckCount} 条待人工复核记录和 ${processingErrorCount} 条真实异常。`;
        }
        if (manualCheckCount > 0) {
            return `已整理 ${successCount} 条成功记录；当前需要你关注的是 ${manualCheckCount} 条待人工复核记录。`;
        }
        if (retentionCount > 0 && processingErrorCount > 0) {
            return `已整理 ${successCount} 条成功记录，另有 ${retentionCount} 条暂存记录和 ${processingErrorCount} 条真实异常需要查看。`;
        }
        if (retentionCount > 0) return `已整理 ${successCount} 条成功记录，另有 ${retentionCount} 条暂存记录可按需导出查看。`;
        if (processingErrorCount > 0) return `已整理 ${successCount} 条成功记录，但仍有 ${processingErrorCount} 条真实异常需要排查。`;
    }
    if (successCount === 0 && pendingCount > 0) {
        if (manualCheckCount > 0) return `本轮暂无可直接归档的记录；请先查看 ${manualCheckCount} 条待人工复核记录，其余结果可按需导出查看。`;
        if (retentionCount > 0 && processingErrorCount > 0) return `本轮暂无可直接归档的记录，当前有 ${retentionCount} 条暂存记录和 ${processingErrorCount} 条真实异常。`;
        if (retentionCount > 0) return "本轮暂无可直接归档的记录，当前结果可通过导出明细进一步查看。";
        if (processingErrorCount > 0) return `本轮暂无可直接归档的记录，当前有 ${processingErrorCount} 条真实异常需要排查。`;
    }
    return "本轮尚未生成可展示的结果记录。";
}

function ResultSummaryCard({ icon, label, value, helper, tone = "info" }) {
    return (
        <section className={joinClasses("stat-card", `stat-card--${tone}`)}>
            <div className="stat-card__top">
                <div className="stat-card__icon"><span className="material-symbols-outlined">{icon}</span></div>
                <div className="stat-card__copy"><p className="stat-card__label">{label}</p><p className="stat-card__value u-tabular">{value}</p></div>
            </div>
            <p className="stat-card__helper">{helper}</p>
        </section>
    );
}

function ResultToolbarButton({ icon, label, onClick, disabled = false, primary = false }) {
    return <button type="button" className={joinClasses("btn", primary ? "btn--primary" : "btn--secondary", "btn--sm")} onClick={onClick} disabled={disabled}><span className="material-symbols-outlined">{icon}</span><span>{label}</span></button>;
}

function ResultActionBanner({ tone = "warning", icon, eyebrow, title, text, buttonLabel, onClick, disabled = false, pathText = "", chips = null }) {
    return (
        <section className={joinClasses("manual-banner", tone === "neutral" && "manual-banner--calm", tone === "neutral" && "manual-banner--output")}>
            <div className="manual-banner__lead">
                <div className="manual-banner__icon"><span className="material-symbols-outlined">{icon}</span></div>
                <div>
                    <p className="manual-banner__eyebrow">{eyebrow}</p>
                    <p className="manual-banner__title">{title}</p>
                    <p className="manual-banner__text">{text}</p>
                    {pathText ? <p className="manual-banner__path u-mono" title={pathText}>{pathText}</p> : null}
                    {chips}
                </div>
            </div>
            <button type="button" className="btn btn--secondary" onClick={onClick} disabled={disabled}>
                <span className="material-symbols-outlined">{icon}</span>
                <span>{buttonLabel}</span>
            </button>
        </section>
    );
}

function AnalysisPage({ onOpenDisclaimer }) {
    const navigate = useNavigate();
    const [summary, setSummary] = useState({});
    const [successInvoices, setSuccessInvoices] = useState([]);
    const [groupedErrors, setGroupedErrors] = useState([]);
    const [manualCheckPath, setManualCheckPath] = useState("");
    const [outputPath, setOutputPath] = useState("");
    const [resultBreakdown, setResultBreakdown] = useState({});
    const [quotaMessage, setQuotaMessage] = useState("");
    const [quotaExhausted, setQuotaExhausted] = useState(false);
    const [lastExportPath, setLastExportPath] = useState("");
    const [loadingError, setLoadingError] = useState("");
    const [exporting, setExporting] = useState(false);

    useEffect(() => {
        let active = true;
        let timer = null;
        const loadResults = async () => {
            try {
                const [results, settingsRes] = await Promise.all([callApi("get_results"), callApi("load_user_settings").catch(() => null)]);
                if (!active || !results) return;
                setSummary(results.summary || {});
                setSuccessInvoices(normalizeSuccessInvoices(results.successInvoices || []));
                setGroupedErrors(normalizeGroupedErrors(results.groupedErrorInvoices || []));
                setManualCheckPath(results.manual_check_path || "");
                setResultBreakdown(results.resultBreakdown || (results.summary && results.summary.result_breakdown) || {});
                setQuotaExhausted(!!results.quota_exhausted);
                setQuotaMessage(results.quota_message || "");
                setLastExportPath(results.last_export_path || "");
                const baseOutput = results.output_path || parentFolder(results.manual_check_path || "") || (settingsRes && settingsRes.settings ? settingsRes.settings.save_path || "" : "");
                setOutputPath(baseOutput);
                setLoadingError("");
            } catch (error) {
                if (active) setLoadingError(error.message || "结果加载失败。");
            }
        };
        loadResults();
        timer = setInterval(loadResults, 3000);
        return () => {
            active = false;
            if (timer) clearInterval(timer);
        };
    }, []);

    const totalErrors = useMemo(() => groupedErrors.reduce((acc, group) => acc + (group.count || group.items.length || 0), 0), [groupedErrors]);
    const successCount = Number(summary.success_count || successInvoices.length);
    const manualReviewGroup = groupedErrors.find((group) => group.key === "manual_review");
    const manualCheckCount = Number(summary.manual_check_count || resultBreakdown.manual_review || (manualReviewGroup && manualReviewGroup.count) || 0);
    const retentionCount = Number(summary.retention_count || resultBreakdown.retained_record || 0);
    const processingErrorCount = Number(summary.processing_error_count || resultBreakdown.processing_error || 0);
    const pendingCount = manualCheckCount + retentionCount + processingErrorCount || Number(summary.error_count || totalErrors || 0);
    const totalResultCount = successCount + pendingCount;
    const statusText = buildResultStatusText(successCount, manualCheckCount, retentionCount, processingErrorCount);
    const groupedVisible = groupedErrors.filter((group) => Number(group.count || group.items.length || 0) > 0);

    async function handleOpenOutput() {
        const target = outputPath || parentFolder(manualCheckPath);
        if (!target) return;
        await callApi("open_folder", target);
    }

    async function handleOpenManualCheck() {
        await callApi("open_manual_check_folder");
    }

    async function openExportedSummary(path) {
        if (!path) throw new Error("结果明细已导出，但未返回文件路径。");
        const openResult = await callApi("view_invoice", path);
        if (!openResult || !openResult.success) throw new Error((openResult && openResult.message) || "结果明细已导出，但打开文件失败。");
    }

    async function handleExport() {
        setExporting(true);
        try {
            const result = await callApi("export_run_summary", outputPath || "");
            if (result && result.success) {
                const exportedPath = result.path || "";
                setLastExportPath(exportedPath);
                await openExportedSummary(exportedPath);
            } else {
                window.alert((result && result.message) || "导出失败。");
            }
        } catch (error) {
            window.alert(error.message || "导出失败。");
        } finally {
            setExporting(false);
        }
    }

    const summaryCards = [
        { key: "success", icon: "task_alt", label: "处理成功", value: successCount, helper: successCount > 0 ? "归档文件已写入输出目录。" : "当前还没有成功记录。", tone: "success" },
        { key: "retention", icon: "inventory_2", label: "暂存记录", value: retentionCount, helper: retentionCount > 0 ? "系统已保全但未纳入成功归档，可按需导出查看。" : "当前没有暂存记录。", tone: "info" },
        { key: "manual", icon: "folder_open", label: "待人工复核", value: manualCheckCount, helper: manualCheckCount > 0 ? "这是当前需要优先处理的内容。" : "当前没有待人工复核项。", tone: manualCheckCount > 0 ? "warning" : "info" },
    ];

    return (
        <AppShell
            active="analysis"
            onOpenDisclaimer={onOpenDisclaimer}
            contentScrollable={false}
            footerLeft={<button type="button" className="btn btn--ghost" onClick={() => navigate("/")}><span className="material-symbols-outlined">add_circle</span><span>开始新批次</span></button>}
            footerRight={lastExportPath ? <p className="footer-meta">最近导出: {fileNameFromPath(lastExportPath)}</p> : null}
        >
            <div className="page-wrap page-wrap--analysis">
                <PageHeader eyebrow={UI_COPY.pages.analysis.eyebrow} title={UI_COPY.pages.analysis.title} />

                <section className="surface-card surface-card--hero">
                    <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
                            <div>
                                <p className="progress-kicker">{UI_COPY.pages.analysis.statusSummary}</p>
                                <h2 className="progress-title">{UI_COPY.pages.analysis.resultTitle}</h2>
                                <p className="progress-meta">共整理 <strong className="u-tabular">{totalResultCount}</strong> 条结果记录。{statusText}</p>
                            </div>
                            <div className="footer-cluster">
                                <ResultToolbarButton icon={exporting ? "sync" : "download"} label={exporting ? "导出中..." : "导出结果明细"} onClick={handleExport} disabled={exporting} primary />
                            </div>
                        </div>
                        <div className="cards-grid">{summaryCards.map((card) => <ResultSummaryCard key={card.key} {...card} />)}</div>
                        {quotaExhausted && quotaMessage ? <NoticeBox tone="warning">{quotaMessage}</NoticeBox> : null}
                        {processingErrorCount > 0 ? <NoticeBox tone="warning">检测到 {processingErrorCount} 条真实异常，请通过“导出结果明细”继续排查。</NoticeBox> : null}
                    </div>
                </section>

                {loadingError ? <NoticeBox tone="error">{loadingError}</NoticeBox> : null}
                {manualCheckCount > 0 ? (
                    <ResultActionBanner
                        icon="folder_open"
                        eyebrow={UI_COPY.pages.analysis.reviewTitle}
                        title={`检测到 ${manualCheckCount} 条待人工复核记录`}
                        text={UI_COPY.pages.analysis.reviewReady}
                        chips={groupedVisible.length > 0 ? <div className="group-strip group-strip--inline">{groupedVisible.map((group) => <span key={group.key} className={joinClasses("group-chip", resolveGroupTone(group.key))}><span>{group.label}</span><span className="u-tabular">{Number(group.count || group.items.length || 0)}</span></span>)}</div> : null}
                        buttonLabel="打开待人工复核"
                        onClick={handleOpenManualCheck}
                        disabled={!manualCheckCount}
                    />
                ) : null}
                <ResultActionBanner
                    tone="neutral"
                    icon="folder"
                    eyebrow="输出目录"
                    title={outputPath ? "归档结果与暂存记录已写入输出目录" : "当前还没有可打开的输出目录"}
                    text={outputPath ? "成功归档文件、暂存记录和导出结果都可以从这里继续查看。" : "待本轮生成结果后，可从这里直接打开输出目录。"}
                    pathText={outputPath || ""}
                    buttonLabel="打开输出目录"
                    onClick={handleOpenOutput}
                    disabled={!outputPath}
                />
            </div>
        </AppShell>
    );
}

function App() {
    const [showDisclaimer, setShowDisclaimer] = useState(false);
    return (
        <>
            <MemoryRouter>
                <Routes>
                    <Route path="/" element={<SettingsPage onOpenDisclaimer={() => setShowDisclaimer(true)} />} />
                    <Route path="/processing" element={<ProcessingPage onOpenDisclaimer={() => setShowDisclaimer(true)} />} />
                    <Route path="/analysis" element={<AnalysisPage onOpenDisclaimer={() => setShowDisclaimer(true)} />} />
                </Routes>
            </MemoryRouter>
            <DisclaimerDialog open={showDisclaimer} onClose={() => setShowDisclaimer(false)} />
        </>
    );
}

const root = createRoot(document.getElementById("root"));
root.render(<App />);
