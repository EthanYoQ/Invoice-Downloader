window.__ModuleLoader__.load({
  id: '@ethanyoq/dsh-invoice-downloader',
  factory: require => {
    const module = { exports: {} }
    const exports = module.exports
    const React = require('react')
    const e = React.createElement

    const name = 'invoice-downloader-client'
    const inject = ['slots', 'connection', 'workspaces']
    let panelOpen = false
    const panelSubscribers = new Set()

    const styles = {
      panel: {
        position: 'fixed',
        zIndex: 50,
        top: '16px',
        right: '16px',
        bottom: '16px',
        left: 'auto',
        width: 'min(440px, calc(100vw - 32px))',
        height: 'auto',
        margin: 0,
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        background: 'var(--dsw-alias-bg-layer-1)',
        color: 'var(--dsw-alias-label-primary)',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: '14px',
        boxShadow: '0 18px 48px rgba(0, 0, 0, 0.42)',
      },
      header: {
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: '12px',
        padding: '16px 18px',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
      },
      title: { fontSize: '16px', fontWeight: 650, lineHeight: 1.35 },
      subtitle: { marginTop: '3px', fontSize: '12px', color: 'var(--dsw-alias-label-tertiary)', lineHeight: 1.45 },
      close: {
        minHeight: '32px',
        padding: '6px 10px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: '8px',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        cursor: 'pointer',
      },
      body: { flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px' },
      form: { display: 'flex', flexDirection: 'column', gap: '18px', minWidth: 0 },
      section: { display: 'flex', flexDirection: 'column', gap: '9px', minWidth: 0 },
      sectionTitle: {
        fontSize: '12px',
        fontWeight: 650,
        letterSpacing: '0.06em',
        color: 'var(--dsw-alias-label-tertiary)',
      },
      field: { display: 'flex', flexDirection: 'column', gap: '6px', minWidth: 0 },
      label: { fontSize: '13px', color: 'var(--dsw-alias-label-primary)' },
      input: {
        boxSizing: 'border-box',
        width: '100%',
        minWidth: 0,
        minHeight: '36px',
        padding: '8px 10px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: '8px',
        background: 'var(--dsw-alias-bg-layer-2)',
        color: 'var(--dsw-alias-label-primary)',
        outline: 'none',
      },
      select: {
        boxSizing: 'border-box',
        width: '100%',
        minHeight: '36px',
        padding: '8px 10px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: '8px',
        background: 'var(--dsw-alias-bg-layer-2)',
        color: 'var(--dsw-alias-label-primary)',
      },
      row: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', minWidth: 0 },
      grow: { flex: '1 1 190px', minWidth: 0 },
      dates: { display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '10px', minWidth: 0 },
      button: {
        minHeight: '36px',
        padding: '8px 12px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: '8px',
        background: 'var(--dsw-alias-button-elevated-fill)',
        color: 'var(--dsw-alias-label-primary)',
        cursor: 'pointer',
        fontSize: '13px',
      },
      primary: {
        background: 'var(--dsw-alias-button-floating-hover)',
        borderColor: 'var(--dsw-alias-border-l2)',
      },
      disabled: { opacity: 0.5, cursor: 'not-allowed' },
      help: { fontSize: '12px', color: 'var(--dsw-alias-label-tertiary)', lineHeight: 1.45 },
      notice: {
        padding: '10px 12px',
        border: '1px solid var(--dsw-alias-border-l1)',
        borderRadius: '8px',
        background: 'var(--dsw-alias-bg-layer-2)',
        color: 'var(--dsw-alias-label-secondary)',
        fontSize: '12px',
        lineHeight: 1.5,
      },
      status: { padding: '8px 10px', borderRadius: '8px', fontSize: '12px', lineHeight: 1.45 },
      success: { background: 'rgba(78, 214, 143, 0.12)', color: '#8ce3b1' },
      error: { background: 'rgba(255, 124, 130, 0.12)', color: '#ff9a9e' },
      working: { background: 'rgba(240, 179, 95, 0.12)', color: '#f0b35f' },
      footerAction: {
        width: '100%',
        minHeight: '36px',
        padding: '8px 12px',
        border: 'none',
        borderRadius: '8px',
        background: 'transparent',
        color: 'var(--dsw-alias-label-secondary)',
        textAlign: 'left',
        cursor: 'pointer',
        overflow: 'hidden',
        whiteSpace: 'nowrap',
        textOverflow: 'ellipsis',
      },
    }

    function usePanelOpen() {
      const [open, setOpen] = React.useState(panelOpen)
      React.useEffect(() => {
        panelSubscribers.add(setOpen)
        return () => panelSubscribers.delete(setOpen)
      }, [])
      const update = React.useCallback(value => {
        panelOpen = value
        panelSubscribers.forEach(listener => listener(value))
      }, [])
      return [open, update]
    }

    function Status({ value }) {
      if (!value || value.kind === 'idle') return null
      const color = value.kind === 'success' ? styles.success : value.kind === 'error' ? styles.error : styles.working
      return e('div', { style: { ...styles.status, ...color }, role: 'status' }, value.message)
    }

    function InvoicePanel({ call, workspaces, onClose }) {
      const [settings, setSettings] = React.useState({
        email: '',
        authCode: '',
        glmApiKey: '',
        dateFrom: '',
        dateTo: '',
        company: '',
        savePath: '',
        ocrProvider: 'local',
      })
      const [runtime, setRuntime] = React.useState({ installed: false, enginePresent: false, platform: '' })
      const [loaded, setLoaded] = React.useState(false)
      const [hasAuthCode, setHasAuthCode] = React.useState(false)
      const [hasGlmApiKey, setHasGlmApiKey] = React.useState(false)
      const [runtimeStatus, setRuntimeStatus] = React.useState({ kind: 'idle', message: '' })
      const [emailStatus, setEmailStatus] = React.useState({ kind: 'idle', message: '' })
      const [pathStatus, setPathStatus] = React.useState({ kind: 'idle', message: '' })
      const [saveStatus, setSaveStatus] = React.useState({ kind: 'idle', message: '' })
      const [scanStatus, setScanStatus] = React.useState({ kind: 'idle', message: '' })
      const [scan, setScan] = React.useState({ running: false, jobId: '', progress: '' })
      const [privacy, setPrivacy] = React.useState('')

      const update = (key, value) => setSettings(previous => ({ ...previous, [key]: value }))
      const refreshRuntime = React.useCallback(async () => {
        const result = await call('getRuntimeStatus')
        if (result.ok) setRuntime(result.value)
      }, [call])

      React.useEffect(() => {
        let alive = true
        Promise.all([
          call('getSettings'),
          call('getRuntimeStatus'),
          call('getPrivacyDisclosure'),
        ]).then(([saved, runtimeResult, privacyResult]) => {
          if (!alive) return
          if (saved.ok) {
            setSettings(previous => ({ ...previous, ...saved.value, authCode: '', glmApiKey: '' }))
            setHasAuthCode(Boolean(saved.value.hasAuthCode))
            setHasGlmApiKey(Boolean(saved.value.hasGlmApiKey))
          }
          if (runtimeResult.ok) setRuntime(runtimeResult.value)
          if (privacyResult.ok) setPrivacy(privacyResult.value.defaultChain || '')
          setLoaded(true)
        }).catch(() => {
          if (alive) setLoaded(true)
        })
        return () => { alive = false }
      }, [call])

      React.useEffect(() => {
        if (!scan.running || !scan.jobId) return undefined
        const timer = setInterval(() => {
          call('getScanStatus', { jobId: scan.jobId }).then(result => {
            if (!result.ok) return
            const next = result.value
            setScan(previous => ({
              running: Boolean(next.running),
              jobId: next.jobId || previous.jobId,
              progress: next.running ? (next.progress || previous.progress) : (next.status || next.progress || previous.progress),
            }))
            if (!next.running) {
              setScanStatus({ kind: next.status === 'completed' ? 'success' : 'error', message: next.status === 'completed' ? '扫描完成。' : '扫描已停止或未完成。' })
            }
          }).catch(() => {})
        }, 1500)
        return () => clearInterval(timer)
      }, [call, scan.jobId, scan.running])

      const installRuntime = async () => {
        setRuntimeStatus({ kind: 'working', message: '正在安装本地引擎；首次安装会下载 Python 依赖和 Chromium。' })
        try {
          const result = await call('installRuntime')
          await refreshRuntime()
          setRuntimeStatus(result.ok
            ? { kind: 'success', message: '本地引擎已准备就绪。' }
            : { kind: 'error', message: result.error?.message || '本地引擎安装失败。' })
        } catch {
          setRuntimeStatus({ kind: 'error', message: '本地引擎安装失败。' })
        }
      }

      const testRuntime = async () => {
        setRuntimeStatus({ kind: 'working', message: '正在检查本地引擎。' })
        try {
          const result = await call('testConnection')
          setRuntimeStatus(result.ok
            ? { kind: 'success', message: '本地引擎连接正常。' }
            : { kind: 'error', message: result.error?.message || '本地引擎连接失败。' })
        } catch {
          setRuntimeStatus({ kind: 'error', message: '本地引擎连接失败。' })
        }
      }

      const chooseDirectory = async () => {
        setPathStatus({ kind: 'idle', message: '' })
        if (!workspaces || typeof workspaces.pickDirectory !== 'function') {
          setPathStatus({ kind: 'error', message: '当前 DSH Host 不支持目录选择。' })
          return
        }
        try {
          const selected = await workspaces.pickDirectory()
          if (typeof selected === 'string' && selected) {
            update('savePath', selected)
            setPathStatus({ kind: 'success', message: '已选择保存位置；保存设置后会重新校验。' })
          }
        } catch {
          setPathStatus({ kind: 'error', message: '无法打开目录选择器。' })
        }
      }

      const save = async () => {
        setSaveStatus({ kind: 'working', message: '正在保存设置。' })
        try {
          const result = await call('saveSettings', settings)
          if (result.ok) {
            setHasAuthCode(Boolean(result.value.hasAuthCode) || hasAuthCode)
            setHasGlmApiKey(Boolean(result.value.hasGlmApiKey) || hasGlmApiKey)
            if (settings.authCode) update('authCode', '')
            if (settings.glmApiKey) update('glmApiKey', '')
          }
          setSaveStatus(result.ok
            ? { kind: 'success', message: '设置已保存。' }
            : { kind: 'error', message: result.error?.message || '设置保存失败。' })
        } catch {
          setSaveStatus({ kind: 'error', message: '设置保存失败。' })
        }
      }

      const testEmail = async () => {
        setEmailStatus({ kind: 'working', message: '正在测试 IMAP 连接。' })
        try {
          const result = await call('testEmailAuth', { email: settings.email, authCode: settings.authCode })
          const body = result.ok ? result.value : { ok: false, message: result.error?.message }
          setEmailStatus({ kind: body.ok ? 'success' : 'error', message: body.message || '邮箱连接失败。' })
        } catch {
          setEmailStatus({ kind: 'error', message: '邮箱连接失败。' })
        }
      }

      const startScan = async () => {
        setScanStatus({ kind: 'working', message: '正在启动扫描。' })
        try {
          const result = await call('startScan', settings)
          if (!result.ok) {
            setScanStatus({ kind: 'error', message: result.error?.message || '扫描未启动。' })
            return
          }
          setScan({ running: true, jobId: result.value.jobId, progress: 'starting' })
          setScanStatus({ kind: 'success', message: '扫描已启动。' })
        } catch {
          setScanStatus({ kind: 'error', message: '扫描未启动。' })
        }
      }

      const stopScan = async () => {
        try {
          await call('stopScan')
          setScanStatus({ kind: 'working', message: '已请求停止扫描。' })
        } catch {
          setScanStatus({ kind: 'error', message: '无法停止扫描。' })
        }
      }

      const disabled = !loaded
      const canScan = runtime.installed && Boolean(
        settings.email
        && (settings.authCode || hasAuthCode)
        && settings.company
        && settings.savePath
        && settings.dateFrom
        && settings.dateTo,
      ) && !scan.running

      return e('aside', {
        style: styles.panel,
        role: 'dialog',
        'aria-label': '发票下载侧边栏',
      },
        e('div', { style: styles.header },
          e('div', null,
            e('div', { style: styles.title }, '发票下载'),
            e('div', { style: styles.subtitle }, '本地 IMAP、OCR、归档与 Excel 汇总'),
          ),
          e('button', { type: 'button', style: styles.close, onClick: onClose }, '关闭'),
        ),
        e('div', { style: styles.body },
          e('div', { style: styles.form },
            e('div', { style: styles.section },
              e('div', { style: styles.sectionTitle }, '本地引擎'),
              e('div', { style: styles.help }, runtime.installed ? '已安装并可进行健康检查。' : '首次使用需要安装本地引擎。'),
              e('div', { style: styles.row },
                e('button', {
                  type: 'button',
                  style: { ...styles.button, ...(!runtime.enginePresent ? styles.disabled : {}) },
                  disabled: !runtime.enginePresent || runtimeStatus.kind === 'working',
                  onClick: installRuntime,
                }, runtimeStatus.kind === 'working' ? '安装中…' : '安装本地引擎'),
                e('button', {
                  type: 'button',
                  style: { ...styles.button, ...(!runtime.installed ? styles.disabled : {}) },
                  disabled: !runtime.installed || runtimeStatus.kind === 'working',
                  onClick: testRuntime,
                }, '测试引擎连接'),
              ),
              e(Status, { value: runtimeStatus }),
            ),
            e('div', { style: styles.section },
              e('div', { style: styles.sectionTitle }, '邮箱配置'),
              e('div', { style: styles.field },
                e('label', { style: styles.label }, '邮箱地址'),
                e('input', {
                  style: styles.input,
                  type: 'email',
                  placeholder: 'name@qq.com 或 name@163.com',
                  disabled,
                  value: settings.email,
                  onInput: event => update('email', event.target.value),
                }),
              ),
              e('div', { style: styles.field },
                e('label', { style: styles.label }, 'IMAP 授权码'),
                e('input', {
                  style: styles.input,
                  type: 'password',
                  placeholder: hasAuthCode ? '已保存；重新输入可替换' : '请输入邮箱授权码',
                  disabled,
                  value: settings.authCode,
                  onInput: event => update('authCode', event.target.value),
                }),
              ),
              e('div', { style: styles.row },
                e('button', {
                  type: 'button',
                  style: { ...styles.button, ...(disabled || emailStatus.kind === 'working' ? styles.disabled : {}) },
                  disabled: disabled || emailStatus.kind === 'working',
                  onClick: testEmail,
                }, emailStatus.kind === 'working' ? '测试中…' : '测试邮箱连接'),
              ),
              e(Status, { value: emailStatus }),
            ),
            e('div', { style: styles.section },
              e('div', { style: styles.sectionTitle }, '扫描范围'),
              e('div', { style: styles.dates },
                e('div', { style: styles.field },
                  e('label', { style: styles.label }, '开始日期'),
                  e('input', { style: styles.input, type: 'date', disabled, value: settings.dateFrom, onInput: event => update('dateFrom', event.target.value) }),
                ),
                e('div', { style: styles.field },
                  e('label', { style: styles.label }, '结束日期'),
                  e('input', { style: styles.input, type: 'date', disabled, value: settings.dateTo, onInput: event => update('dateTo', event.target.value) }),
                ),
              ),
              e('div', { style: styles.field },
                e('label', { style: styles.label }, '公司名称'),
                e('input', {
                  style: styles.input,
                  type: 'text',
                  placeholder: '用于匹配购买方',
                  disabled,
                  value: settings.company,
                  onInput: event => update('company', event.target.value),
                }),
              ),
            ),
            e('div', { style: styles.section },
              e('div', { style: styles.sectionTitle }, '保存位置'),
              e('div', { style: styles.row },
                e('input', {
                  style: { ...styles.input, ...styles.grow },
                  type: 'text',
                  placeholder: '绝对保存路径',
                  disabled,
                  value: settings.savePath,
                  onInput: event => update('savePath', event.target.value),
                }),
                e('button', {
                  type: 'button',
                  style: { ...styles.button, ...(disabled ? styles.disabled : {}) },
                  disabled,
                  onClick: chooseDirectory,
                }, '选择保存位置'),
              ),
              e('div', { style: styles.help }, '也可手动输入绝对路径；保存时由 Host 创建并校验该目录。'),
              e(Status, { value: pathStatus }),
            ),
            e('div', { style: styles.section },
              e('div', { style: styles.sectionTitle }, '识别方式'),
              e('select', {
                style: styles.select,
                disabled,
                value: settings.ocrProvider,
                  onChange: event => update('ocrProvider', event.target.value),
              },
                e('option', { value: 'local' }, '本地 RapidOCR + 当前模型'),
                e('option', { value: 'glm' }, 'GLM 视觉识别（可选）'),
              ),
              settings.ocrProvider === 'glm'
                ? e('div', { style: styles.field },
                  e('label', { style: styles.label }, 'GLM API Key'),
                  e('input', {
                    style: styles.input,
                    type: 'password',
                    placeholder: hasGlmApiKey ? '已保存；重新输入可替换' : '可选',
                    disabled,
                    value: settings.glmApiKey,
                  onInput: event => update('glmApiKey', event.target.value),
                  }),
                )
                : null,
            ),
            privacy ? e('div', { style: styles.notice }, privacy) : null,
            e('div', { style: styles.row },
              e('button', {
                type: 'button',
                style: { ...styles.button, ...(disabled || saveStatus.kind === 'working' ? styles.disabled : {}) },
                disabled: disabled || saveStatus.kind === 'working',
                onClick: save,
              }, saveStatus.kind === 'working' ? '保存中…' : '保存设置'),
              e('button', {
                type: 'button',
                style: { ...styles.button, ...styles.primary, ...(!canScan ? styles.disabled : {}) },
                disabled: !canScan,
                onClick: startScan,
              }, '开始扫描'),
              scan.running ? e('button', { type: 'button', style: styles.button, onClick: stopScan }, '停止扫描') : null,
            ),
            e(Status, { value: saveStatus }),
            scan.progress ? e('div', { style: styles.help }, `当前状态：${scan.progress}`) : null,
            e(Status, { value: scanStatus }),
          ),
        ),
      )
    }

    function FooterAction({ wide }) {
      const [open, setOpen] = usePanelOpen()
      return e('button', {
        type: 'button',
        title: '打开发票下载',
        'aria-label': '打开发票下载',
        'aria-pressed': open,
        style: { ...styles.footerAction, ...(open ? styles.primary : {}) },
        onClick: () => setOpen(true),
      }, wide ? '发票下载' : '发票')
    }

    function Overlay({ call, workspaces }) {
      const [open, setOpen] = usePanelOpen()
      return open ? e(InvoicePanel, { call, workspaces, onClose: () => setOpen(false) }) : null
    }

    function apply(ctx) {
      const connection = ctx.get('connection')
      const workspaces = ctx.workspaces
      const call = (endpoint, payload) => connection.rpc.call('/invoice', endpoint, payload ?? {})
      ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register(
        { name: 'sidebar.footer.action', id: 'invoice-downloader', order: 95, label: '发票下载' },
        props => e(FooterAction, { wide: Boolean(props && props.wide) }),
      ))
      ctx.slots.inject('shell.overlay', () => ctx.slots.register(
        { name: 'shell.overlay', id: 'invoice-downloader-panel', order: 95, label: '发票下载' },
        () => e(Overlay, { call, workspaces }),
      ))
    }

    exports.apply = apply
    exports.inject = inject
    exports.name = name
    return module.exports
  },
})
