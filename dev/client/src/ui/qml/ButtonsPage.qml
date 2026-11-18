/*
 * ButtonsPage.qml - 按键页
 * ---------------------------------------------------------------------------
 * ★ 严格照抄参考项目 ★
 *   reference/winmic-src/apps/windows/rc003/src/ovb_rc003/qml/ButtonsPage.qml
 *   （1601 行）。逐段对照关系：
 *
 *   参考 1138-1175  顶部型号分段条  → 改成「已配对遥控器」分段条
 *   参考 1177-1199  Flickable + 竖直 ScrollBar
 *   参考 1207-1243  双 Canvas（普通连线 / 选中那条线）
 *   参考 1245-1312  左列 GridLayout（Layout.row 物理行对齐）
 *   参考 1314-1464  实物图 + 热点（按 paintedWidth/paintedHeight 定位）
 *   参考 1466-1533  右列 GridLayout
 *   参考 1536-1594  底部操作条 SectionFrame
 *   参考  217-234   恢复型号默认确认框
 *   参考  237-554   快捷键录入弹窗（按键录入 / 手动输入双模式）
 *   参考  853-1089  动作编辑器 3 列（手势 / 可编辑分组下拉 / 备注）
 *   参考 1091-1102  校验错误条
 *   参考   32-44    草稿脏标记 + 提交 / 丢弃
 *
 * 本项目与参考项目的差别：
 *   - 参考项目按「型号」分段（RC003 / Chrome），我们按「遥控器实例」分段
 *   - 映射写入直接调 Bridge.setMapping / Bridge.setMappingNote
 *     （后端负责「本机存一份备份 + 同步给板子」）
 *   - ★ 改完即自动保存 ★ —— 没有「保存」按钮，每次改下拉或备注立刻写回
 *
 * ★ 连接线必须画在 Canvas 的 onPaint 里 ★
 *   v0.16.0 的教训：在 onPaint 之外拿 ctx 画，RHI 渲染下 requestPaint()
 *   会把画布刷成全黑。所以这里只有 onPaint 里调 paintConnectors()。
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
import QtQuick.Dialogs

Item {
    id: root
    property var tokens

    // ======================================================== 布局常量
    readonly property real mappingCardGap: 2
    readonly property real mappingBoardGap: 6
    readonly property real mappingPhotoColumnWidth: 114
    readonly property int mappingCardColumnWidth: Math.floor(
        (mappingList.width - mappingPhotoColumnWidth - mappingBoardGap * 2) / 2)
    readonly property real mappingKeyLabelWidth: 56
    readonly property int leftCardRows: 7
    readonly property int rightCardRows: 8
    readonly property int remoteButtonCount: Bridge.buttonList.length

    property bool connectorRepaintQueued: false
    property string _localSelectedId: ""
    property var _noteCache: ({})

    // 后端还没上 setSelectedButtonId 时的本地兜底
    readonly property string selectedButtonId: {
        var v = undefined
        try { v = Bridge.selectedButtonId } catch (e) { v = undefined }
        if (v === undefined || v === null || v === "")
            return root._localSelectedId
        return String(v)
    }

    // ======================================================== 备注缓存
    // 后端把备注存进了 display_notes，但 buttonList 目前只回 single_note；
    // 双击 / 长按的备注先缓存在 QML 侧，写入后立即回显。
    function noteFieldFor(trigger) {
        if (trigger === "double_click")
            return "double_note"
        if (trigger === "long_press")
            return "long_note"
        return "single_note"
    }

    function noteFor(row, trigger) {
        if (!row)
            return ""
        const cached = root._noteCache[row.key_id + "|" + trigger]
        if (cached !== undefined)
            return cached
        const v = row[noteFieldFor(trigger)]
        if (v === undefined || v === null)
            return ""
        return String(v)
    }

    function cacheNote(keyId, trigger, note) {
        const next = {}
        for (const k in root._noteCache)
            next[k] = root._noteCache[k]
        next[keyId + "|" + trigger] = note
        root._noteCache = next
    }

    function clearNoteCache() {
        root._noteCache = ({})
    }

    /* 备注缓存是按遥控器分的 —— 换了遥控器就得清掉 */
    property int _cacheRemoteIndex: -1

    function syncNoteCacheRemote() {
        const idx = Bridge.currentRemote
        if (idx !== root._cacheRemoteIndex) {
            root._cacheRemoteIndex = idx
            root.clearNoteCache()
        }
    }

    // ======================================================== 后端写入
    function rowOf(buttonId) {
        const all = Bridge.buttonList
        for (let i = 0; i < all.length; i++)
            if (all[i].button_id === buttonId)
                return all[i]
        return null
    }

    function isLeftButton(buttonId) {
        const row = root.rowOf(buttonId)
        return row !== null && row.side === "left"
    }

    function writeAction(buttonId, trigger, text) {
        const row = root.rowOf(buttonId)
        if (!row)
            return false
        let value = text === undefined || text === null ? "" : String(text).trim()
        // 「未设置」不是动作 —— 存空（后端记成「禁用」）
        if (value === "未设置")
            value = ""
        let ok = false
        try { ok = Bridge.setMapping(row.key_id, trigger, value) !== false } catch (e) { ok = false }
        return ok
    }

    /*
     * ★ setMappingNote 的签名探测 ★
     *   新签名是 setMappingNote(key_id, trigger, note)（三个参数），
     *   旧签名是 setMappingNote(key_id, note)（两个参数）。
     *   少于三个参数去调新签名，Qt 会抛 "Insufficient arguments"；
     *   旧签名则正常返回 —— 用这一点区分两者。
     *   探测用的是空备注 + 假 key，不会留下任何数据。
     */
    property bool _noteApiProbed: false
    property bool _noteApiTriggerAware: false

    function noteApiSupportsTrigger() {
        if (root._noteApiProbed)
            return root._noteApiTriggerAware
        root._noteApiProbed = true
        let aware = false
        try {
            Bridge.setMappingNote("__probe__", "")
            aware = false
        } catch (e) {
            aware = true
        }
        root._noteApiTriggerAware = aware
        return aware
    }

    function writeNote(buttonId, trigger, note) {
        const row = root.rowOf(buttonId)
        if (!row)
            return false
        root.cacheNote(row.key_id, trigger, note)
        let ok = false
        if (root.noteApiSupportsTrigger()) {
            try {
                ok = Bridge.setMappingNote(row.key_id, trigger, note) !== false
            } catch (e) {
                ok = false
            }
            if (!ok && trigger === "single_click") {
                try { ok = Bridge.setMappingNote(row.key_id, note) !== false } catch (e) { ok = false }
            }
        } else if (trigger === "single_click") {
            try { ok = Bridge.setMappingNote(row.key_id, note) !== false } catch (e) { ok = false }
        }
        return ok
    }

    function selectButton(buttonId) {
        root._localSelectedId = buttonId
        try { Bridge.setSelectedButtonId(buttonId) } catch (e) { }
        root.scheduleConnectorRepaint()
    }

    // ======================================================== 连接线
    function connectorControlRadius(startX, endX) {
        const span = Math.abs(endX - startX)
        const preferred = Math.max(12, Math.min(72, span * 0.56))
        return Math.min(preferred, span * 0.48)
    }

    function connectorStrokeColor(active) {
        if (active)
            return tokens.accent
        return tokens.borderStrong
    }

    function connectorRoute(card, hotspot, coordinateItem) {
        if (!card || !hotspot)
            return null
        const leftSide = root.isLeftButton(hotspot.buttonId)
        const start = card.mapToItem(
            coordinateItem,
            leftSide ? card.width : 0,
            card.height / 2
        )
        const center = hotspot.mapToItem(
            coordinateItem,
            hotspot.width / 2,
            hotspot.height / 2
        )
        // 连线停在热点边缘，别把键上的字盖住
        const endX = center.x + (hotspot.buttonId === "ok" ? 0
            : (leftSide ? -1 : 1) * hotspot.width / 2)
        const endY = center.y - (hotspot.buttonId === "ok"
            ? hotspot.height / 2 : 0)
        const direction = leftSide ? 1 : -1
        // 一条不打断的三次贝塞尔，端点处切线水平
        const controlRadius = root.connectorControlRadius(start.x, endX)
        return {
            startX: start.x,
            startY: start.y,
            control1X: start.x + direction * controlRadius,
            control1Y: start.y,
            control2X: endX - direction * controlRadius,
            control2Y: endY,
            endX: endX,
            endY: endY
        }
    }

    function paintConnectors(canvas, activeOnly) {
        const ctx = canvas.getContext("2d")
        ctx.reset()
        const buttons = Bridge.buttonList
        for (let i = 0; i < buttons.length; i++) {
            const buttonId = buttons[i].button_id
            const hotspot = photoHotspotRepeater.itemAt(i)
            if (!hotspot)
                continue
            const card = root.isLeftButton(buttonId)
                ? leftCardRepeater.itemAt(i) : rightCardRepeater.itemAt(i)
            if (!card || !hotspot || !card.visible || !hotspot.visible)
                continue
            const active = root.selectedButtonId === buttonId
            if (active !== activeOnly)
                continue
            const route = root.connectorRoute(card, hotspot, canvas)
            if (!route)
                continue
            ctx.beginPath()
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.moveTo(route.startX, route.startY)
            ctx.bezierCurveTo(
                route.control1X,
                route.control1Y,
                route.control2X,
                route.control2Y,
                route.endX,
                route.endY
            )
            ctx.strokeStyle = root.connectorStrokeColor(active)
            ctx.lineWidth = active ? 2.2 : 1.25
            ctx.stroke()
        }
    }

    function scheduleConnectorRepaint() {
        if (root.connectorRepaintQueued)
            return
        root.connectorRepaintQueued = true
        Qt.callLater(function() {
            root.connectorRepaintQueued = false
            mappingLines.requestPaint()
            activeMappingLine.requestPaint()
        })
    }

    onWidthChanged: scheduleConnectorRepaint()
    onHeightChanged: scheduleConnectorRepaint()
    Component.onCompleted: {
        syncNoteCacheRemote()
        scheduleConnectorRepaint()
    }

    onVisibleChanged: {
        if (visible) {
            Qt.callLater(root.scheduleConnectorRepaint)
        } else {
            // 切页走人：先把草稿提交掉
            root.commitPendingEditorDraft()
            shortcutRecorder.requestClose()
        }
    }

    Connections {
        target: Bridge
        function onMappingsChanged() { root.scheduleConnectorRepaint() }
        function onRemotesChanged() {
            root.syncNoteCacheRemote()
            root.scheduleConnectorRepaint()
        }
    }

    // ======================================================== 名称
    function shortButtonName(buttonId) {
        const names = {
            "power": "电源", "up": "上", "left": "左", "back": "返回",
            "home": "主页", "mic": "语音", "right": "右", "ok": "确定",
            "down": "下", "volume_up": "+", "volume_down": "-",
            "volume_mute": "静音", "youtube": "YouTube",
            "netflix": "Netflix", "input_source": "输入源"
        }
        return names[buttonId] !== undefined ? names[buttonId] : buttonId
    }

    function triggerLabel(trigger) {
        if (trigger === "double_click")
            return "双击"
        if (trigger === "long_press")
            return "长按"
        return "单击"
    }

    // ======================================================== 快捷键文本
    /*
     * 手动输入的校验（参考项目 normalizeMappingHotkey_text 的精简版）：
     * 键名认不出来就报错，Ctrl+Alt+Delete 直接拒绝。
     */
    function canonicalKeyToken(raw) {
        const p = String(raw).trim().toLowerCase()
        const chinese = {
            "方向上": "up", "方向下": "down", "方向左": "left", "方向右": "right",
            "回车": "enter", "回车键": "enter", "退格": "backspace",
            "空格": "space", "删除": "delete", "esc": "escape",
            "主页": "home", "结束": "end", "插入": "insert",
            "制表": "tab", "翻页上": "pageup", "翻页下": "pagedown"
        }
        if (chinese[p] !== undefined)
            return chinese[p]
        const modifiers = {
            "ctrl": "ctrl", "control": "ctrl", "左ctrl": "ctrl",
            "shift": "shift", "左shift": "shift",
            "alt": "alt", "左alt": "alt",
            "win": "win", "cmd": "win", "左win": "win", "super": "win",
            // 录制出来的是带左右的（lwin+lctrl…），必须原样认
            "lctrl": "lctrl", "rctrl": "rctrl",
            "lshift": "lshift", "rshift": "rshift",
            "lalt": "lalt", "ralt": "ralt",
            "lwin": "lwin", "rwin": "rwin"
        }
        if (modifiers[p] !== undefined)
            return modifiers[p]
        const punctuation = {
            ",": "comma", "逗号": "comma",
            ".": "period", "句号": "period",
            "/": "slash", "斜杠": "slash",
            "\\": "backslash", "反斜杠": "backslash",
            ";": "semicolon", "分号": "semicolon",
            "'": "quote", "引号": "quote",
            "-": "minus", "减号": "minus",
            "=": "equal", "等号": "equal",
            "`": "grave", "反引号": "grave",
            "[": "bracketleft", "左方括号": "bracketleft",
            "]": "bracketright", "右方括号": "bracketright"
        }
        if (punctuation[p] !== undefined)
            return punctuation[p]
        const known = [
            "backspace", "tab", "enter", "escape", "space", "pageup",
            "pagedown", "end", "home", "left", "up", "right", "down",
            "insert", "delete", "apps", "printscreen",
            "comma", "period", "slash", "backslash", "semicolon",
            "quote", "minus", "equal", "grave",
            "bracketleft", "bracketright",
            "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
            "f11", "f12"
        ]
        if (known.indexOf(p) >= 0)
            return p
        if (/^[a-z0-9]$/.test(p))
            return p
        return ""
    }

    function normalizeHotkeyText(text) {
        const raw = text === undefined || text === null ? "" : String(text).trim()
        if (raw.length === 0)
            return { ok: false, text: "", message: qsTr("快捷键不能为空") }
        const parts = raw.split("+")
        const tokens = []
        for (let i = 0; i < parts.length; i++) {
            const token = root.canonicalKeyToken(parts[i])
            if (token.length === 0)
                return { ok: false, text: "",
                         message: qsTr("无法识别的按键：") + parts[i].trim() }
            if (tokens.indexOf(token) < 0)
                tokens.push(token)
        }
        const hasCtrl = tokens.indexOf("ctrl") >= 0
        const hasAlt = tokens.indexOf("alt") >= 0
        const hasDelete = tokens.indexOf("delete") >= 0
        if (hasCtrl && hasAlt && hasDelete)
            return { ok: false, text: "",
                     message: qsTr("Ctrl+Alt+Delete 是 Windows 安全按键，软件不能模拟。") }
        return { ok: true, text: tokens.join("+"), message: "" }
    }

    function actionOptionsFor(buttonId, trigger) {
        if (trigger === "double_click")
            return Bridge.secondaryActionOptionsFor(buttonId)
        if (trigger === "long_press")
            return Bridge.longPressActionOptionsFor(buttonId)
        return Bridge.primaryActionOptionsFor(buttonId)
    }

    function actionErrorFor(buttonId, trigger, text) {
        const value = text === undefined || text === null ? "" : String(text).trim()
        if (value.length === 0)
            return ""
        const options = root.actionOptionsFor(buttonId, trigger)
        for (let i = 0; i < options.length; i++)
            if (String(options[i]) === value)
                return ""
        if (root.normalizeHotkeyText(value).ok)
            return ""
        return qsTr("无法识别的动作：") + value
    }

    /* 后端把「没配」存成空串，编辑器里要显示成下拉里的那一项 */
    function normalizeEditorText(trigger, text) {
        const value = text === undefined || text === null ? "" : String(text).trim()
        if (value.length > 0)
            return value
        return trigger === "single_click" ? "禁用" : "未设置"
    }

    // ======================================================== 草稿
    readonly property bool hasPendingEditorDraft:
        actionEditor.visible && actionEditor.draftDirty

    function commitPendingEditorDraft() {
        if (!actionEditor.visible)
            return true
        return actionEditor.commitDraft()
    }

    function discardPendingEditorDraft() {
        if (actionEditor.visible)
            actionEditor.discardDraft()
    }

    // ======================================================== 页面底色
    // 少了它，这一页是透明的 —— Canvas 一旦异常就整块发黑
    Rectangle {
        anchors.fill: parent
        color: root.tokens.background
        z: -1
    }


    // ======================================================== 恢复默认确认框
    Dialog {
        id: restoreMappingDefaultsDialog
        objectName: "restoreMappingDefaultsDialog"
        title: qsTr("恢复型号默认？")
        modal: true
        anchors.centerIn: parent
        // ★ 必须写死宽度 ★
        //   不写的话弹窗宽度 ← contentItem 的 implicitWidth ←
        //   contentItem 的宽度 ← 弹窗宽度 —— 死循环。
        width: 380
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: Bridge.restoreMappingDefaults()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: qsTr("这会把当前遥控器的 %1 个按键恢复为内置映射，并立即自动保存。另一台的设置不变。")
                .arg(root.remoteButtonCount)
        }
    }

    // ======================================================== 快捷键录入弹窗
    Dialog {
        id: shortcutRecorder
        objectName: "shortcutRecorderDialog"
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(430, root.width - 28)
        readonly property string headerText: qsTr("录入快捷键")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: 14
        rightPadding: 14
        topPadding: 0
        bottomPadding: 11
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.NoAutoClose
        property string buttonId: ""
        property string trigger: "single_click"
        property string previewText: ""
        property bool pendingClose: false
        property string pendingChord: ""
        property string inputMode: "capture"
        property string manualErrorText: ""

        function beginCapture() {
            previewText = qsTr("请按下希望遥控器发送的键盘快捷键")
            captureArea.forceActiveFocus()
            Bridge.startHotkeyCapture()
        }

        function selectCaptureMode() {
            if (inputMode === "capture")
                return
            inputMode = "capture"
            manualErrorText = ""
            beginCapture()
        }

        function selectManualMode() {
            if (inputMode === "manual")
                return
            if (Bridge.hotkeyCapturing)
                Bridge.stopHotkeyCapture()
            inputMode = "manual"
            manualErrorText = ""
            Qt.callLater(function() {
                manualShortcutField.forceActiveFocus()
            })
        }

        function commitManualShortcut() {
            const result = root.normalizeHotkeyText(manualShortcutField.text)
            if (!result.ok) {
                manualErrorText = result.message
                manualShortcutField.forceActiveFocus()
                return
            }
            manualErrorText = ""
            commitShortcut(result.text)
        }

        function commitShortcut(chord) {
            const result = root.normalizeHotkeyText(chord)
            if (!result.ok) {
                pendingChord = ""
                selectManualMode()
                manualShortcutField.text = chord
                manualErrorText = result.message
                return
            }
            previewText = result.text
            pendingChord = result.text
            requestClose()
        }

        function finishClose() {
            if (pendingChord.length > 0 && actionEditor.visible)
                actionEditor.applyCaptured(trigger, pendingChord)
            pendingChord = ""
            pendingClose = false
            close()
        }

        function requestClose() {
            pendingClose = true
            if (!Bridge.hotkeyCapturing) {
                finishClose()
                return true
            }
            Bridge.stopHotkeyCapture()
            if (!Bridge.hotkeyCapturing)
                finishClose()
            return true
        }

        onOpened: {
            pendingClose = false
            pendingChord = ""
            inputMode = "capture"
            manualErrorText = ""
            manualShortcutField.text = ""
            beginCapture()
        }

        onClosed: {
            // ★ 关闭时必须停掉录制 ★ 钩子一直挂着会吞掉全系统键盘输入
            if (Bridge.hotkeyCapturing)
                Bridge.stopHotkeyCapture()
            pendingClose = false
            pendingChord = ""
            manualErrorText = ""
        }

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.width: root.tokens.hairlineWidth
            border.color: root.tokens.border
        }

        header: Item {
            width: shortcutRecorder.width
            implicitHeight: 38

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                tokens: root.tokens
                kind: sectionTitleKind
                text: shortcutRecorder.headerText
                font.weight: Font.Medium
            }

            DialogCloseButton {
                id: shortcutRecorderCloseButton
                objectName: "shortcutRecorderCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                onCloseRequested: shortcutRecorder.requestClose()
            }
        }

        Connections {
            target: Bridge
            function onHotkeyCaptureChanged() {
                if (!shortcutRecorder.visible)
                    return
                if (shortcutRecorder.inputMode === "capture" && !Bridge.hotkeyCapturing
                        && Bridge.capturedHotkey.length > 0) {
                    shortcutRecorder.commitShortcut(Bridge.capturedHotkey)
                }
            }
        }

        contentItem: FocusScope {
            id: captureArea
            implicitHeight: 164
            focus: true
            Keys.onEscapePressed: shortcutRecorder.requestClose()

            ColumnLayout {
                anchors.fill: parent
                spacing: root.tokens.spacingMedium

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    CompactButton {
                        id: captureModeButton
                        objectName: "shortcutRecorderCaptureModeButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        checkable: true
                        checked: shortcutRecorder.inputMode === "capture"
                        highlighted: checked
                        text: qsTr("按键录入")
                        onClicked: shortcutRecorder.selectCaptureMode()
                    }
                    CompactButton {
                        id: manualModeButton
                        objectName: "shortcutRecorderManualModeButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        checkable: true
                        checked: shortcutRecorder.inputMode === "manual"
                        highlighted: checked
                        text: qsTr("手动输入")
                        onClicked: shortcutRecorder.selectManualMode()
                    }
                }

                ColumnLayout {
                    visible: shortcutRecorder.inputMode === "capture"
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    UiLabel {
                        tokens: root.tokens
                        kind: sectionTitleKind
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: shortcutRecorder.previewText
                        color: root.tokens.accent
                    }
                    UiLabel {
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: qsTr("按下要发送的单键或组合键")
                        elide: Text.ElideRight
                    }
                }

                ColumnLayout {
                    visible: shortcutRecorder.inputMode === "manual"
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingTiny

                    CompactTextField {
                        id: manualShortcutField
                        objectName: "shortcutRecorderManualField"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        enabled: !Bridge.hotkeyCapturing
                        placeholderText: qsTr("例如 Ctrl+[、Win+L 或 左 Ctrl+←")
                        selectByMouse: true
                        onTextChanged: shortcutRecorder.manualErrorText = ""
                        onAccepted: shortcutRecorder.commitManualShortcut()
                        Accessible.name: qsTr("手动输入快捷键")
                    }
                    UiLabel {
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: shortcutRecorder.manualErrorText.length > 0
                            ? shortcutRecorder.manualErrorText
                            : qsTr("可直接修改键名；用 + 连接，确认时检查；单独 Win 按左 Win 发送")
                        color: shortcutRecorder.manualErrorText.length > 0
                            ? root.tokens.errorColor : root.tokens.textSecondary
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        objectName: "shortcutRecorderCancelButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: qsTr("取消")
                        onClicked: shortcutRecorder.requestClose()
                    }
                    CompactButton {
                        objectName: "shortcutRecorderManualConfirmButton"
                        tokens: root.tokens
                        visible: shortcutRecorder.inputMode === "manual"
                        enabled: manualShortcutField.text.trim().length > 0
                            && !Bridge.hotkeyCapturing
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: qsTr("确定")
                        highlighted: true
                        onClicked: shortcutRecorder.commitManualShortcut()
                    }
                }
            }
        }
    }

    // ======================================================== 动作编辑器
    Dialog {
        id: actionEditor
        objectName: "actionEditorDialog"
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(430, root.width - root.tokens.spacingLarge * 2)
        readonly property string headerText: buttonName.length > 0
            ? qsTr("编辑：") + buttonName
            : qsTr("编辑")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: 14
        rightPadding: 14
        topPadding: 0
        bottomPadding: 11
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.CloseOnEscape

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.width: root.tokens.hairlineWidth
            border.color: root.tokens.border
        }

        header: Item {
            width: actionEditor.width
            implicitHeight: 34

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                tokens: root.tokens
                kind: sectionTitleKind
                text: actionEditor.headerText
                font.pixelSize: root.tokens.fontSizeTitle
                font.weight: Font.Medium
            }

            DialogCloseButton {
                id: actionEditorCloseButton
                objectName: "actionEditorCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                onCloseRequested: actionEditor.discardDraft()
            }
        }

        property string buttonId: ""
        property string buttonName: ""
        property string primaryText: ""
        property string doubleText: "未设置"
        property string longText: "未设置"
        property string primaryNote: ""
        property string doubleNote: ""
        property string longNote: ""
        property string originalPrimaryText: ""
        property string originalDoubleText: "未设置"
        property string originalLongText: "未设置"
        property string originalPrimaryNote: ""
        property string originalDoubleNote: ""
        property string originalLongNote: ""
        property bool syncing: false
        property bool validationRequested: false

        readonly property string primaryValidationError:
            root.actionErrorFor(buttonId, "single_click", primaryText)
        readonly property string doubleValidationError:
            root.actionErrorFor(buttonId, "double_click", doubleText)
        readonly property string longValidationError:
            root.actionErrorFor(buttonId, "long_press", longText)
        readonly property string validationError:
            primaryValidationError.length > 0 ? primaryValidationError
            : doubleValidationError.length > 0 ? doubleValidationError
            : longValidationError
        readonly property bool draftDirty:
            primaryText !== originalPrimaryText
            || doubleText !== originalDoubleText
            || longText !== originalLongText
            || primaryNote !== originalPrimaryNote
            || doubleNote !== originalDoubleNote
            || longNote !== originalLongNote

        function openForButton(buttonIdValue) {
            const row = root.rowOf(buttonIdValue)
            if (!row || row.is_voice)
                return
            syncing = true
            validationRequested = false
            buttonId = buttonIdValue
            buttonName = root.shortButtonName(buttonIdValue)
            primaryText = root.normalizeEditorText("single_click", row.single)
            doubleText = root.normalizeEditorText("double_click", row.double)
            longText = root.normalizeEditorText("long_press", row.long)
            primaryNote = root.noteFor(row, "single_click")
            doubleNote = root.noteFor(row, "double_click")
            longNote = root.noteFor(row, "long_press")
            originalPrimaryText = primaryText
            originalDoubleText = doubleText
            originalLongText = longText
            originalPrimaryNote = primaryNote
            originalDoubleNote = doubleNote
            originalLongNote = longNote
            primaryCombo.setEditorValue(primaryText)
            doubleCombo.setEditorValue(doubleText)
            longCombo.setEditorValue(longText)
            primaryNoteField.text = primaryNote
            doubleNoteField.text = doubleNote
            longNoteField.text = longNote
            syncing = false
            open()
            primaryCombo.forceActiveFocus()
        }

        /* 录完 / 手输完的快捷键直接落到对应的格子里 */
        function applyCaptured(trigger, chord) {
            if (!visible)
                return
            syncing = true
            if (trigger === "single_click") {
                primaryText = chord
                primaryCombo.setEditorValue(chord)
            } else if (trigger === "double_click") {
                doubleText = chord
                doubleCombo.setEditorValue(chord)
            } else if (trigger === "long_press") {
                longText = chord
                longCombo.setEditorValue(chord)
            }
            syncing = false
            saveChanges()
        }

        /* ★ 改完即自动保存 ★ —— 只写**真正改过**的那一项，
           免得每动一下就把整套映射重写一遍（后端每次写都要推给板子） */
        function saveChanges() {
            if (buttonId.length === 0)
                return false
            let ok = true
            if (primaryText !== originalPrimaryText) {
                if (!root.writeAction(buttonId, "single_click", primaryText))
                    ok = false
                originalPrimaryText = primaryText
            }
            if (doubleText !== originalDoubleText) {
                if (!root.writeAction(buttonId, "double_click", doubleText))
                    ok = false
                originalDoubleText = doubleText
            }
            if (longText !== originalLongText) {
                if (!root.writeAction(buttonId, "long_press", longText))
                    ok = false
                originalLongText = longText
            }
            if (primaryNote !== originalPrimaryNote) {
                root.writeNote(buttonId, "single_click", primaryNote)
                originalPrimaryNote = primaryNote
            }
            if (doubleNote !== originalDoubleNote) {
                root.writeNote(buttonId, "double_click", doubleNote)
                originalDoubleNote = doubleNote
            }
            if (longNote !== originalLongNote) {
                root.writeNote(buttonId, "long_press", longNote)
                originalLongNote = longNote
            }
            root.scheduleConnectorRepaint()
            return ok
        }

        function commitDraft() {
            if (buttonId.length === 0)
                return false
            validationRequested = true
            const ok = actionEditor.saveChanges()
            if (!ok && validationError.length > 0)
                return false
            close()
            return true
        }

        /* 放弃本次编辑 —— 把值还原成打开时的样子 */
        function discardDraft() {
            if (buttonId.length > 0 && draftDirty) {
                const changedPrimary = primaryText !== originalPrimaryText
                const changedDouble = doubleText !== originalDoubleText
                const changedLong = longText !== originalLongText
                const changedPrimaryNote = primaryNote !== originalPrimaryNote
                const changedDoubleNote = doubleNote !== originalDoubleNote
                const changedLongNote = longNote !== originalLongNote
                syncing = true
                primaryText = originalPrimaryText
                doubleText = originalDoubleText
                longText = originalLongText
                primaryNote = originalPrimaryNote
                doubleNote = originalDoubleNote
                longNote = originalLongNote
                primaryCombo.setEditorValue(primaryText)
                doubleCombo.setEditorValue(doubleText)
                longCombo.setEditorValue(longText)
                primaryNoteField.text = primaryNote
                doubleNoteField.text = doubleNote
                longNoteField.text = longNote
                syncing = false
                if (changedPrimary)
                    root.writeAction(buttonId, "single_click", primaryText)
                if (changedDouble)
                    root.writeAction(buttonId, "double_click", doubleText)
                if (changedLong)
                    root.writeAction(buttonId, "long_press", longText)
                if (changedPrimaryNote)
                    root.writeNote(buttonId, "single_click", primaryNote)
                if (changedDoubleNote)
                    root.writeNote(buttonId, "double_click", doubleNote)
                if (changedLongNote)
                    root.writeNote(buttonId, "long_press", longNote)
                root.scheduleConnectorRepaint()
            }
            close()
        }

        onClosed: {
            syncing = true
            buttonId = ""
        }

        contentItem: ColumnLayout {
            spacing: 9

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: root.tokens.spacingMedium
                rowSpacing: root.tokens.spacingSmall

                Item {
                    Layout.preferredWidth: 34
                    Layout.minimumWidth: 34
                    Layout.maximumWidth: 34
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    text: qsTr("按键录入")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    text: qsTr("备注名称")
                }

                /* ---- 单击 */
                UiLabel {
                    id: primaryGestureTitle
                    objectName: "actionEditorPrimaryTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("单击")
                    font.pixelSize: root.tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: primaryGestureTitleHover }
                    CompactToolTip {
                        objectName: "actionEditorPrimaryHelp"
                        tokens: root.tokens
                        active: primaryGestureTitleHover.hovered
                        text: qsTr("可选普通动作、快捷键或自定义组合键；下拉框里也能直接打字")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    EditorActionCombo {
                        id: primaryCombo
                        objectName: "actionEditorPrimaryCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        model: Bridge.primaryActionOptionsFor(actionEditor.buttonId)
                        Accessible.name: actionEditor.buttonName + qsTr("单击动作")
                        onEditTextChanged: {
                            if (!actionEditor.syncing) {
                                actionEditor.primaryText = editText
                                actionEditor.validationRequested = false
                            }
                        }
                        onActivated: {
                            const selectedText = String(model[index])
                            actionEditor.syncing = true
                            editText = selectedText
                            actionEditor.syncing = false
                            actionEditor.primaryText = selectedText
                            actionEditor.saveChanges()
                        }
                        // 手动打的字：离开输入框就写回
                        onActiveFocusChanged: {
                            if (!activeFocus && !actionEditor.syncing)
                                actionEditor.saveChanges()
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorPrimaryRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: qsTr("录入")
                        onClicked: root.openShortcutRecorder(
                            actionEditor.buttonId, "single_click")
                        Accessible.name: qsTr("录制单击快捷键")
                    }
                }
                CompactTextField {
                    id: primaryNoteField
                    objectName: "actionEditorPrimaryNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing) {
                            actionEditor.primaryNote = text
                            actionEditor.validationRequested = false
                        }
                    }
                    onEditingFinished: {
                        if (!actionEditor.syncing)
                            actionEditor.saveChanges()
                    }
                    Accessible.name: qsTr("单击备注名称")
                }

                /* ---- 双击 */
                UiLabel {
                    id: doubleGestureTitle
                    objectName: "actionEditorDoubleTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("双击")
                    font.pixelSize: root.tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: doubleGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: doubleGestureTitleHover.hovered
                        text: qsTr("会等待约 0.3 秒区分单击和双击；设置双击或长按后，此键不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    EditorActionCombo {
                        id: doubleCombo
                        objectName: "actionEditorDoubleCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        model: Bridge.secondaryActionOptionsFor(actionEditor.buttonId)
                        Accessible.name: actionEditor.buttonName + qsTr("双击动作")
                        onEditTextChanged: {
                            if (!actionEditor.syncing) {
                                actionEditor.doubleText = editText
                                actionEditor.validationRequested = false
                            }
                        }
                        onActivated: {
                            const selectedText = String(model[index])
                            actionEditor.syncing = true
                            editText = selectedText
                            actionEditor.syncing = false
                            actionEditor.doubleText = selectedText
                            actionEditor.saveChanges()
                        }
                        // 手动打的字：离开输入框就写回
                        onActiveFocusChanged: {
                            if (!activeFocus && !actionEditor.syncing)
                                actionEditor.saveChanges()
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorDoubleRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: qsTr("录入")
                        onClicked: root.openShortcutRecorder(
                            actionEditor.buttonId, "double_click")
                        Accessible.name: qsTr("录制双击快捷键")
                    }
                }
                CompactTextField {
                    id: doubleNoteField
                    objectName: "actionEditorDoubleNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing) {
                            actionEditor.doubleNote = text
                            actionEditor.validationRequested = false
                        }
                    }
                    onEditingFinished: {
                        if (!actionEditor.syncing)
                            actionEditor.saveChanges()
                    }
                    Accessible.name: qsTr("双击备注名称")
                }

                /* ---- 长按 */
                UiLabel {
                    id: longGestureTitle
                    objectName: "actionEditorLongTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("长按")
                    font.pixelSize: root.tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: longGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: longGestureTitleHover.hovered
                        text: qsTr("按住约 0.55 秒触发；设置后不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall

                    EditorActionCombo {
                        id: longCombo
                        objectName: "actionEditorLongCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        model: Bridge.longPressActionOptionsFor(actionEditor.buttonId)
                        Accessible.name: actionEditor.buttonName + qsTr("长按动作")
                        onEditTextChanged: {
                            if (!actionEditor.syncing) {
                                actionEditor.longText = editText
                                actionEditor.validationRequested = false
                            }
                        }
                        onActivated: {
                            const selectedText = String(model[index])
                            actionEditor.syncing = true
                            editText = selectedText
                            actionEditor.syncing = false
                            actionEditor.longText = selectedText
                            actionEditor.saveChanges()
                        }
                        // 手动打的字：离开输入框就写回
                        onActiveFocusChanged: {
                            if (!activeFocus && !actionEditor.syncing)
                                actionEditor.saveChanges()
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorLongRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: qsTr("录入")
                        onClicked: root.openShortcutRecorder(
                            actionEditor.buttonId, "long_press")
                        Accessible.name: qsTr("录制长按快捷键")
                    }
                }
                CompactTextField {
                    id: longNoteField
                    objectName: "actionEditorLongNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing) {
                            actionEditor.longNote = text
                            actionEditor.validationRequested = false
                        }
                    }
                    onEditingFinished: {
                        if (!actionEditor.syncing)
                            actionEditor.saveChanges()
                    }
                    Accessible.name: qsTr("长按备注名称")
                }
            }

            UiLabel {
                id: actionEditorValidationError
                objectName: "actionEditorValidationError"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                visible: text.length > 0
                text: actionEditor.validationRequested ? actionEditor.validationError : ""
                color: root.tokens.errorColor
                wrapMode: Text.WordWrap
                Accessible.name: text
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "actionEditorCancelButton"
                    tokens: root.tokens
                    compactMinimumWidth: root.tokens.buttonWidth2Chars
                    text: qsTr("取消")
                    onClicked: actionEditor.discardDraft()
                }
                CompactButton {
                    objectName: "actionEditorSaveButton"
                    tokens: root.tokens
                    compactMinimumWidth: root.tokens.buttonWidth2Chars
                    text: qsTr("完成")
                    enabled: actionEditor.buttonId.length > 0
                    highlighted: true
                    onClicked: actionEditor.commitDraft()
                }
            }
        }
    }

    // ======================================================== 打开录入弹窗
    function openShortcutRecorder(buttonId, trigger) {
        shortcutRecorder.buttonId = buttonId
        shortcutRecorder.trigger = trigger || "single_click"
        shortcutRecorder.previewText = qsTr("请按下希望遥控器发送的键盘快捷键")
        shortcutRecorder.open()
    }

    function openEditor(buttonId) {
        const row = root.rowOf(buttonId)
        if (!row || row.is_voice)
            return
        root.selectButton(buttonId)
        actionEditor.openForButton(buttonId)
    }

    // ======================================================== 主体
    Item {
        id: mappingLayout
        objectName: "mappingLayout"
        anchors.fill: parent

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: root.tokens.pageHorizontalPadding
            spacing: root.tokens.spacingSmall

            // ---------------------------------------- 顶部遥控器切换条
            SectionFrame {
                id: mappingViewSwitcher
                objectName: "mappingViewSwitcher"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                horizontalPadding: 3
                verticalPadding: 3
                radius: root.tokens.cornerRadiusSmall

                RowLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 3

                    Repeater {
                        model: RemoteListModel
                        delegate: CompactButton {
                            required property int index
                            required property string name
                            tokens: root.tokens
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            text: name.length > 0
                                ? name : qsTr("遥控器 ") + (index + 1)
                            highlighted: index === Bridge.currentRemote
                            subduedText: !highlighted
                            onClicked: Bridge.selectRemote(index)
                            Accessible.name: qsTr("切换遥控器：") + text
                        }
                    }

                    UiLabel {
                        objectName: "noRemoteHint"
                        visible: RemoteListModel.count === 0
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: qsTr("还没有配对遥控器 —— 先到「设备」页把遥控器配对上")
                    }
                }
            }

            // ---------------------------------------- 主区（Flickable）
            Flickable {
                id: mappingList
                objectName: "mappingList"
                clip: true
                contentWidth: width
                contentHeight: Math.max(height, leftSideCards.implicitHeight,
                                        rightSideCards.implicitHeight,
                                        photoSidebar.implicitHeight)
                flickableDirection: Flickable.VerticalFlick
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {
                    objectName: "mappingScrollBar"
                    policy: ScrollBar.AsNeeded
                    // 放在页面留白里，别压在右边的卡片上
                    parent: mappingLayout
                    x: mappingList.parent.x + mappingList.x + mappingList.width
                    y: mappingList.parent.y + mappingList.y
                    height: mappingList.height
                }
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 306
                onWidthChanged: root.scheduleConnectorRepaint()
                onHeightChanged: root.scheduleConnectorRepaint()

                Canvas {
                    id: mappingLines
                    objectName: "mappingLines"
                    anchors.fill: parent
                    z: 0
                    antialiasing: true

                    onPaint: root.paintConnectors(mappingLines, false)

                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()
                }

                Canvas {
                    id: activeMappingLine
                    objectName: "activeMappingLine"
                    anchors.fill: parent
                    z: 2
                    antialiasing: true
                    onPaint: root.paintConnectors(activeMappingLine, true)
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()
                }

                GridLayout {
                    id: leftSideCards
                    objectName: "leftMappingCards"
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    width: root.mappingCardColumnWidth
                    columns: 1
                    rows: root.leftCardRows
                    rowSpacing: root.mappingCardGap
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: leftCardRepeater
                        model: Bridge.buttonList
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property var modelData

                            visible: modelData.side === "left"
                            enabled: !modelData.is_voice
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: modelData.button_id
                            // ★ 物理行对齐 ★ 不能只按顺序排
                            Layout.row: modelData.visual_row
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? implicitHeight : 0
                            buttonName: root.shortButtonName(modelData.button_id)
                            keyLabelWidth: root.mappingKeyLabelWidth
                            singleText: modelData.single
                            doubleText: modelData.double
                            longText: modelData.long
                            singleNoteText: root.noteFor(modelData, "single_click")
                            doubleNoteText: root.noteFor(modelData, "double_click")
                            longNoteText: root.noteFor(modelData, "long_press")
                            selected: root.selectedButtonId === modelData.button_id
                            voiceAction: modelData.is_voice
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: root.openEditor(modelData.button_id)
                        }
                    }
                }

                Item {
                    id: photoSidebar
                    objectName: "photoSidebar"
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    width: root.mappingPhotoColumnWidth
                    height: photoFrame.height + 20
                    z: 1
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Item {
                        id: photoFrame
                        objectName: "photoFrame"
                        anchors.top: parent.top
                        anchors.horizontalCenter: parent.horizontalCenter
                        /*
                         * ★ 照抄参考项目：框 86×250 ★
                         *   以前按整列 114 宽算高度（363），
                         *   实物图比参考项目大一圈，两边卡片被挤窄，
                         *   字就挤在一起、看着不协调。
                         */
                        width: 86
                        height: 250
                        onXChanged: root.scheduleConnectorRepaint()
                        onYChanged: root.scheduleConnectorRepaint()
                        onWidthChanged: root.scheduleConnectorRepaint()
                        onHeightChanged: root.scheduleConnectorRepaint()

                        Image {
                            id: photoImage
                            objectName: "remotePhoto"
                            /*
                             * ★ 照抄参考项目的画法 ★
                             *   - 宽度按原图比例算（453×1443 → 78×250）
                             *   - mipmap: true —— 缩 5.8 倍没有 mipmap 就是糊 + 摩尔纹，
                             *     这就是「实物图不清晰」的真因（不是图的问题）
                             *   - MultiEffect 投影：遥控器浮起来，不发灰
                             */
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.verticalCenter: parent.verticalCenter
                            height: parent.height
                            width: height * sourceSize.width
                                   / Math.max(1, sourceSize.height)
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            smooth: true
                            mipmap: true
                            source: Bridge.remoteImageSource(Bridge.currentRemote)
                            layer.enabled: photoImage.status === Image.Ready
                            layer.effect: MultiEffect {
                                shadowEnabled: true
                                shadowColor: root.tokens.remotePhotoShadowColor
                                shadowBlur: 1.0
                                blurMax: root.tokens.remotePhotoShadowRadius
                                shadowHorizontalOffset: 0
                                shadowVerticalOffset: root.tokens.remotePhotoShadowOffset
                            }
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                        }

                        UiLabel {
                            objectName: "photoMissingHint"
                            anchors.centerIn: parent
                            width: parent.width
                            visible: photoImage.status !== Image.Ready
                            tokens: root.tokens
                            kind: noteKind
                            text: qsTr("实物图缺失")
                            horizontalAlignment: Text.AlignHCenter
                        }

                        Repeater {
                            id: photoHotspotRepeater
                            model: root.hotspotRows
                            onItemAdded: root.scheduleConnectorRepaint()
                            delegate: Item {
                                id: photoHotspot
                                objectName: "photoHotspot_" + modelData.button_id

                                required property var modelData
                                property string buttonId: modelData.button_id
                                readonly property bool isSelected:
                                    root.selectedButtonId === modelData.button_id

                                // ★ 热点按 paintedWidth / paintedHeight 定位 ★
                                //   图是 PreserveAspectFit 摆的，控件宽高 ≠ 图的宽高
                                width: modelData.width * photoImage.paintedWidth
                                height: modelData.height * photoImage.paintedHeight
                                x: photoImage.x
                                    + (photoImage.width - photoImage.paintedWidth) / 2
                                    + modelData.x * photoImage.paintedWidth
                                    - width / 2
                                y: photoImage.y
                                    + (photoImage.height - photoImage.paintedHeight) / 2
                                    + modelData.y * photoImage.paintedHeight
                                    - height / 2
                                visible: modelData.hasHotspot
                                    && photoImage.status === Image.Ready
                                z: 2
                                onXChanged: root.scheduleConnectorRepaint()
                                onYChanged: root.scheduleConnectorRepaint()
                                onWidthChanged: root.scheduleConnectorRepaint()
                                onVisibleChanged: root.scheduleConnectorRepaint()
                                Component.onCompleted: root.scheduleConnectorRepaint()

                                Rectangle {
                                    objectName: "photoHotspotMarker_" + photoHotspot.buttonId
                                    anchors.fill: parent
                                    z: 1
                                    visible: photoHotspot.isSelected
                                    radius: Math.min(width, height) / 2
                                    color: photoHotspot.modelData.is_voice
                                        ? Qt.rgba(root.tokens.voiceAccent.r,
                                                  root.tokens.voiceAccent.g,
                                                  root.tokens.voiceAccent.b, 0.24)
                                        : Qt.rgba(root.tokens.accent.r,
                                                  root.tokens.accent.g,
                                                  root.tokens.accent.b, 0.20)
                                    border.width: 2
                                    border.color: photoHotspot.modelData.is_voice
                                        ? root.tokens.voiceAccent : root.tokens.accent
                                }

                                TapHandler {
                                    onTapped: root.openEditor(photoHotspot.buttonId)
                                }
                                HoverHandler { id: hotspotHover }
                                Rectangle {
                                    anchors.fill: parent
                                    z: 1
                                    visible: hotspotHover.hovered
                                        && !photoHotspot.isSelected
                                    radius: Math.min(width, height) / 2
                                    color: Qt.rgba(root.tokens.accent.r,
                                                   root.tokens.accent.g,
                                                   root.tokens.accent.b, 0.10)
                                    border.color: root.tokens.accent
                                }
                            }
                        }

                        Connections {
                            target: photoImage
                            function onPaintedWidthChanged() {
                                root.scheduleConnectorRepaint()
                            }
                            function onPaintedHeightChanged() {
                                root.scheduleConnectorRepaint()
                            }
                        }
                    }
                }

                GridLayout {
                    id: rightSideCards
                    objectName: "rightMappingCards"
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    width: root.mappingCardColumnWidth
                    columns: 1
                    rows: root.rightCardRows
                    rowSpacing: root.mappingCardGap
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: rightCardRepeater
                        model: Bridge.buttonList
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property var modelData

                            visible: modelData.side !== "left"
                            enabled: !modelData.is_voice
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: modelData.button_id
                            Layout.row: modelData.visual_row
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? implicitHeight : 0
                            buttonName: root.shortButtonName(modelData.button_id)
                            keyLabelWidth: root.mappingKeyLabelWidth
                            singleText: modelData.single
                            doubleText: modelData.double
                            longText: modelData.long
                            singleNoteText: root.noteFor(modelData, "single_click")
                            doubleNoteText: root.noteFor(modelData, "double_click")
                            longNoteText: root.noteFor(modelData, "long_press")
                            selected: root.selectedButtonId === modelData.button_id
                            voiceAction: modelData.is_voice
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: root.openEditor(modelData.button_id)
                        }
                    }
                }
            }

            // ---------------------------------------- 底部操作条
            SectionFrame {
                id: mappingActionsPanel
                objectName: "mappingActionsPanel"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                horizontalPadding: 4
                verticalPadding: 3
                radius: root.tokens.cornerRadiusSmall

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 4

                    CompactButton {
                        id: detectRealKeyButton
                        objectName: "detectRealKeyButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: Bridge.learningActive
                            ? qsTr("停止检测") : qsTr("检测真实按键")
                        highlighted: Bridge.learningActive
                        onClicked: {
                            if (Bridge.learningActive)
                                Bridge.stopLearn()
                            else
                                Bridge.startLearn()
                        }
                        Accessible.name: qsTr("检测真实遥控器按键")
                    }
                    UiLabel {
                        objectName: "keyDetectHint"
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        text: Bridge.learningActive
                            ? qsTr("请按一下遥控器上要识别的键")
                            : (Bridge.learnResultText.length > 0
                               ? qsTr("已检测：") + Bridge.learnResultText
                                 + qsTr("；修改完成后会自动保存")
                               : qsTr("检测真实按键：按一下遥控器上的键，会自动选中对应的那一行"))
                        elide: Text.ElideRight
                    }
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        id: customAppsButton
                        objectName: "customAppsButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: qsTr("启动应用…")
                        onClicked: { customAppDialog.reload(); customAppDialog.open() }
                    }
                    CompactButton {
                        id: restoreMappingDefaultsButton
                        objectName: "restoreMappingDefaultsButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: qsTr("恢复型号默认")
                        onClicked: restoreMappingDefaultsDialog.open()
                    }
                    CompactButton {
                        id: saveModelDefaultButton
                        objectName: "saveModelDefaultButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: qsTr("存为型号默认")
                        onClicked: Bridge.saveModelDefault()
                    }
                    CompactButton {
                        id: exportMappingButton
                        objectName: "exportMappingButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: qsTr("导出配置")
                        enabled: !Bridge.learningActive
                        onClicked: exportMappingDialog.open()
                    }
                    CompactButton {
                        id: importMappingButton
                        objectName: "importMappingButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: qsTr("导入配置")
                        enabled: !Bridge.learningActive
                        onClicked: importMappingDialog.open()
                    }
                }
            }
        }
    }

    // ======================================================== 热点数据
    /*
     * 热点坐标和 buttonList 按 button_id 对齐，
     * 保证卡片列和热点列的下标一一对应（画线时靠下标配对）。
     */
    readonly property var hotspotRows: {
        const buttons = Bridge.buttonList
        const spots = Bridge.hotspots
        const out = []
        for (let i = 0; i < buttons.length; i++) {
            const b = buttons[i]
            let found = null
            for (let j = 0; j < spots.length; j++) {
                if (spots[j].button_id === b.button_id) {
                    found = spots[j]
                    break
                }
            }
            out.push({
                button_id: b.button_id,
                hasHotspot: found !== null,
                x: found ? found.x : 0,
                y: found ? found.y : 0,
                width: found ? found.width : 0,
                height: found ? found.height : 0,
                is_voice: b.is_voice
            })
        }
        return out
    }

    FileDialog {
        id: exportMappingDialog
        objectName: "exportMappingDialog"
        title: qsTr("导出按键配置")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("按键配置 (*.json)"), qsTr("所有文件 (*)")]
        defaultSuffix: "json"
        onAccepted: Bridge.exportMappingFileTo(selectedFile.toString())
    }

    FileDialog {
        id: importMappingDialog
        objectName: "importMappingDialog"
        title: qsTr("导入按键配置")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("按键配置 (*.json)"), qsTr("所有文件 (*)")]
        onAccepted: Bridge.importMappingFile(selectedFile.toString())
    }

    /*
     * ★ 自定义启动项 ★
     *   名字 + 程序/快捷方式路径，数量不限；加完就出现在
     *   「启动应用」下拉组的末尾，选中即可绑定。
     */
    Dialog {
        id: customAppDialog
        objectName: "customAppDialog"
        title: qsTr("自定义启动项")
        modal: true
        anchors.centerIn: parent
        width: 620
        padding: 0

        property var apps: []
        function reload() { apps = Bridge.customApps() }

        contentItem: ColumnLayout {
            spacing: 10
            anchors.fill: parent
            anchors.margins: 16

            Text {
                Layout.fillWidth: true
                text: qsTr("填一个名字和程序或快捷方式的路径，它就会出现在「启动应用」下拉里。数量不限。")
                font.pixelSize: root.tokens.fontSizeControl
                color: root.tokens.textSecondary
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: customAppDialog.apps
                delegate: RowLayout {
                    Layout.fillWidth: true
                    spacing: root.tokens.spacingSmall
                    Text {
                        text: modelData.label
                        Layout.preferredWidth: 150
                        elide: Text.ElideRight
                        font.pixelSize: root.tokens.fontSizeControl
                    }
                    Text {
                        text: modelData.path
                        Layout.fillWidth: true
                        elide: Text.ElideMiddle
                        color: root.tokens.textSecondary
                        font.pixelSize: root.tokens.fontSizeControl
                    }
                    CompactButton {
                        tokens: root.tokens
                        text: qsTr("删除")
                        onClicked: {
                            Bridge.removeCustomApp(modelData.label)
                            customAppDialog.reload()
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.tokens.spacingSmall
                CompactTextField {
                    id: newAppLabelField
                    tokens: root.tokens
                    Layout.preferredWidth: 150
                    placeholderText: qsTr("名称")
                }
                CompactTextField {
                    id: newAppPathField
                    tokens: root.tokens
                    Layout.fillWidth: true
                    placeholderText: qsTr("程序路径 / 快捷方式")
                }
                CompactButton {
                    tokens: root.tokens
                    text: qsTr("浏览")
                    onClicked: pickAppDialog.open()
                }
                CompactButton {
                    tokens: root.tokens
                    text: qsTr("添加")
                    onClicked: {
                        if (Bridge.addCustomApp(newAppLabelField.text,
                                                newAppPathField.text)) {
                            newAppLabelField.text = ""
                            newAppPathField.text = ""
                            customAppDialog.reload()
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                CompactButton {
                    tokens: root.tokens
                    text: qsTr("关闭")
                    onClicked: customAppDialog.close()
                }
            }
        }
    }

    FileDialog {
        id: pickAppDialog
        objectName: "pickAppDialog"
        title: qsTr("选择要启动的程序")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("程序 (*.exe *.lnk *.bat *.cmd)"), qsTr("所有文件 (*)")]
        onAccepted: {
            newAppPathField.text = Bridge.localPath(selectedFile.toString())
            if (newAppLabelField.text.length === 0)
                newAppLabelField.text = Bridge.baseName(selectedFile.toString())
        }
    }
}
