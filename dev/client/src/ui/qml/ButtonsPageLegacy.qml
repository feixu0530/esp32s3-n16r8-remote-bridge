/*
 * ButtonsPage.qml - 按键页
 * ---------------------------------------------------------------------------
 * 照抄参考项目 windows-remote-mic-app 的 ButtonsPage.qml（1601 行）结构：
 *
 *     ┌─ 左卡片列 7 行 ─┐   ┌─ 遥控器实物图 114px ─┐   ┌─ 右卡片列 8 行 ─┐
 *     │  键名 + 三格     │   │  + 15 个热点          │   │  键名 + 三格     │
 *     │  单击/双击/长按  │   │  + 贝塞尔连接线       │   │  单击/双击/长按  │
 *     └──────────────────┘   └──────────────────────┘   └──────────────────┘
 *
 * 每张卡三格 = 单击 / 双击 / 长按，点一下打开动作编辑器
 * （3 列 GridLayout：手势标题 / 可编辑分组下拉 + 录入 / 备注）。
 *
 * ★ 映射规则全部写回开发板 ★
 *   每次保存走 MAPBEGIN / MAPSET / MAPCOMMIT，板子存 NVS，拔掉 PC 也生效。
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

Item {
    id: root
    property var tokens

    property string selectedButtonId: ""
    property var cardItems: ({})
    property var spotItems: ({})

    // ---- 照抄参考项目的布局常量 ----
    readonly property real mappingCardGap: 2
    readonly property real mappingBoardGap: 6
    readonly property real mappingPhotoColumnWidth: 114
    readonly property real mappingKeyLabelWidth: 56
    readonly property int leftRows: 7
    readonly property int rightRows: 8

    readonly property var leftCards: root._cards("left")
    readonly property var rightCards: root._cards("right")

    function _cards(side) {
        var out = []
        var all = Bridge.buttonList
        for (var i = 0; i < all.length; i++) {
            if (all[i].side === side)
                out.push(all[i])
        }
        out.sort(function (a, b) { return a.visual_row - b.visual_row })
        return out
    }

    function rowOf(buttonId) {
        var all = Bridge.buttonList
        for (var i = 0; i < all.length; i++) {
            if (all[i].button_id === buttonId)
                return all[i]
        }
        return null
    }

    function triggerLabel(trigger) {
        if (trigger === "double_click")
            return "双击"
        if (trigger === "long_press")
            return "长按"
        return "单击"
    }

    // ---- 连接线（照抄参考项目 connectorRoute / connectorControlRadius）----
    function connectorControlRadius(startX, endX) {
        const span = Math.abs(endX - startX)
        const preferred = Math.max(12, Math.min(72, span * 0.56))
        return Math.min(preferred, span * 0.48)
    }

    function connectorRoute(card, hotspot, coordinateItem) {
        if (!card || !hotspot)
            return null
        const leftSide = root.cardItems[hotspot.buttonId] === card
        const start = card.mapToItem(coordinateItem,
                                     leftSide ? card.width : 0,
                                     card.height / 2)
        const center = hotspot.mapToItem(coordinateItem,
                                         hotspot.width / 2, hotspot.height / 2)
        const endX = center.x + (hotspot.buttonId === "ok"
                                  ? 0
                                  : (leftSide ? -1 : 1) * hotspot.width / 2)
        const endY = center.y - (hotspot.buttonId === "ok"
                                  ? hotspot.height / 2 : 0)
        const direction = leftSide ? 1 : -1
        const controlRadius = root.connectorControlRadius(start.x, endX)
        return {
            startX: start.x, startY: start.y,
            control1X: start.x + direction * controlRadius,
            control1Y: start.y,
            control2X: endX - direction * controlRadius,
            control2Y: endY,
            endX: endX, endY: endY
        }
    }

    /*
     * ★ 连接线必须画在 onPaint 里 ★
     *   之前在 paintConnectors() 里直接取 ctx 画完再 requestPaint()：
     *   离屏截图（软件渲染）看着正常，RHI 渲染的真窗口里 requestPaint()
     *   会把画布重刷成**全黑** —— 整个按键区一片黑，遥控器图也被盖住。
     */
    function paintConnectors() {
        mappingLines.requestPaint()
    }

    function scheduleConnectorRepaint() {
        if (root._repaintQueued)
            return
        root._repaintQueued = true
        Qt.callLater(function () {
            root._repaintQueued = false
            root.paintConnectors()
        })
    }
    property bool _repaintQueued: false

    onVisibleChanged: {
        if (visible)
            Qt.callLater(root.scheduleConnectorRepaint)
    }

    onWidthChanged: scheduleConnectorRepaint()
    onHeightChanged: scheduleConnectorRepaint()

    Connections {
        target: Bridge
        function onMappingsChanged() { root.scheduleConnectorRepaint() }
    }

    // ==================================================== 主体
    // 页面底色 —— 少了它，这一页是透明的，Canvas 一旦异常就整块发黑
    Rectangle {
        anchors.fill: parent
        color: root.tokens.background
        z: -1
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ---------------------------------------------- 主区
        Item {
            id: mappingArea
            Layout.fillWidth: true
            Layout.fillHeight: true

            Canvas {
                id: mappingLines
                objectName: "mappingLines"
                anchors.fill: parent
                z: 0

                onPaint: {
                    var ctx = getContext("2d")
                    ctx.reset()
                    var all = Bridge.buttonList
                    for (var i = 0; i < all.length; i++) {
                        const buttonId = all[i].button_id
                        const card = root.cardItems[buttonId]
                        const hotspot = root.spotItems[buttonId]
                        if (!card || !hotspot || !card.visible || !hotspot.visible)
                            continue
                        const active = root.selectedButtonId === buttonId
                        const route = root.connectorRoute(card, hotspot, mappingLines)
                        if (!route)
                            continue
                        ctx.beginPath()
                        ctx.lineCap = "round"
                        ctx.lineJoin = "round"
                        ctx.moveTo(route.startX, route.startY)
                        ctx.bezierCurveTo(route.control1X, route.control1Y,
                                          route.control2X, route.control2Y,
                                          route.endX, route.endY)
                        ctx.strokeStyle = active
                            ? root.tokens.accent : root.tokens.borderStrong
                        ctx.lineWidth = active ? 2.2 : 1.25
                        ctx.stroke()
                    }
                }
            }

            GridLayout {
                id: leftGrid
                objectName: "leftMappingCards"
                columns: 1
                rows: root.leftRows
                columnSpacing: 0
                rowSpacing: root.mappingCardGap
                z: 3
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                width: root.cardColumnWidth

                Repeater {
                    id: leftRepeater
                    model: root.leftCards
                    delegate: MappingCard {
                        required property var modelData
                        tokens: root.tokens
                        // ★ 必须撑满整列 ★
                        //   不写这句，AbstractButton 按 implicitWidth 缩成 60px，
                        //   三格（单击/双击/长按）全被挤到卡片外面去。
                        Layout.fillWidth: true
                        cardId: modelData.button_id
                        buttonName: modelData.button_name
                        singleText: modelData.single
                        doubleText: modelData.double
                        longText: modelData.long
                        singleNoteText: modelData.single_note
                        keyLabelWidth: root.mappingKeyLabelWidth
                        selected: root.selectedButtonId === modelData.button_id
                        voiceAction: modelData.is_voice
                        enabled: !modelData.is_voice
                        Component.onCompleted: {
                            root.cardItems[modelData.button_id] = this
                            root.scheduleConnectorRepaint()
                        }
                        onXChanged: root.scheduleConnectorRepaint()
                        onYChanged: root.scheduleConnectorRepaint()
                        onWidthChanged: root.scheduleConnectorRepaint()
                        onHeightChanged: root.scheduleConnectorRepaint()
                        onClicked: {
                            root.selectedButtonId = modelData.button_id
                            root.openEditor(modelData.button_id, "single_click")
                            root.scheduleConnectorRepaint()
                        }
                    }
                }
            }

            // ---- 实物图 + 热点
            Item {
                id: photoSidebar
                objectName: "photoSidebar"
                z: 1
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                width: root.mappingPhotoColumnWidth
                /*
                 * ★ 高度不能反过来由 photoFrame 决定 ★
                 *   原来 photoSidebar.height = photoFrame.height + 20，
                 *   photoFrame.height 又按 parent.height 算 —— 互为因果，
                 *   解出来是 0 / -20：实物图整个看不见，15 个热点全挤在正中间。
                 */
                height: root.mappingPhotoColumnWidth * 1443 / 453 + 20

                Rectangle {
                    id: photoFrame
                    objectName: "photoFrame"
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    width: root.mappingPhotoColumnWidth
                    height: parent.height - 20

                    Image {
                        id: remotePhoto
                        objectName: "remotePhoto"
                        anchors.fill: parent
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        source: Bridge.remoteImageSource(Bridge.currentRemote)
                    }

                    Repeater {
                        id: hotspotRepeater
                        model: Bridge.hotspots
                        delegate: Rectangle {
                            required property var modelData
                            objectName: "photoHotspot_" + modelData.button_id
                            visible: remotePhoto.status === Image.Ready
                            width: modelData.width * remotePhoto.paintedWidth
                            height: modelData.height * remotePhoto.paintedHeight
                            x: remotePhoto.x
                               + (remotePhoto.width - remotePhoto.paintedWidth) / 2
                               + modelData.x * remotePhoto.paintedWidth
                               - width / 2
                            y: remotePhoto.y
                               + (remotePhoto.height - remotePhoto.paintedHeight) / 2
                               + modelData.y * remotePhoto.paintedHeight
                               - height / 2
                            radius: width / 2
                            color: root.selectedButtonId === modelData.button_id
                                   ? tokens.accent : tokens.accentSoft
                            border.color: tokens.accent
                            border.width: 1
                            opacity: 0.24

                            property string buttonId: modelData.button_id

                            Component.onCompleted: {
                                root.spotItems[modelData.button_id] = this
                                root.scheduleConnectorRepaint()
                            }
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()

                            TapHandler {
                                onTapped: {
                                    root.selectedButtonId = modelData.button_id
                                    root.openEditor(modelData.button_id, "single_click")
                                    root.scheduleConnectorRepaint()
                                }
                            }
                        }
                    }
                }
            }

            GridLayout {
                id: rightGrid
                objectName: "rightMappingCards"
                columns: 1
                rows: root.rightRows
                columnSpacing: 0
                rowSpacing: root.mappingCardGap
                z: 3
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: root.cardColumnWidth

                Repeater {
                    model: root.rightCards
                    delegate: MappingCard {
                        required property var modelData
                        tokens: root.tokens
                        Layout.fillWidth: true
                        cardId: modelData.button_id
                        buttonName: modelData.button_name
                        singleText: modelData.single
                        doubleText: modelData.double
                        longText: modelData.long
                        singleNoteText: modelData.single_note
                        keyLabelWidth: root.mappingKeyLabelWidth
                        selected: root.selectedButtonId === modelData.button_id
                        voiceAction: modelData.is_voice
                        enabled: !modelData.is_voice
                        Component.onCompleted: {
                            root.cardItems[modelData.button_id] = this
                            root.scheduleConnectorRepaint()
                        }
                        onXChanged: root.scheduleConnectorRepaint()
                        onYChanged: root.scheduleConnectorRepaint()
                        onWidthChanged: root.scheduleConnectorRepaint()
                        onHeightChanged: root.scheduleConnectorRepaint()
                        onClicked: {
                            root.selectedButtonId = modelData.button_id
                            root.openEditor(modelData.button_id, "single_click")
                            root.scheduleConnectorRepaint()
                        }
                    }
                }
            }
        }

        // ---------------------------------------------- 底部动作条
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            color: tokens.surface

            Rectangle {
                width: parent.width
                height: 1
                color: tokens.border
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: tokens.spacingSmall

                CompactButton {
                    objectName: "detectRealKeyButton"
                    tokens: root.tokens
                    text: "检测真实按键"
                    enabled: Bridge.currentRemote >= 0
                    onClicked: {
                        if (Bridge.learningActive) {
                            Bridge.stopLearn()
                        } else {
                            Bridge.startLearn()
                        }
                    }
                }

                UiLabel {
                    objectName: "keyDetectHint"
                    tokens: root.tokens
                    kind: 1
                    Layout.fillWidth: true
                    text: Bridge.learningActive
                        ? "请按一下遥控器上要识别的键"
                        : "检测真实按键：按一下遥控器上的键，板子会报出它的键码"
                }

                CompactButton {
                    objectName: "restoreDefaultsButton"
                    tokens: root.tokens
                    text: "恢复内置默认"
                    onClicked: restoreDialog.open()
                }

                CompactButton {
                    objectName: "managePresetsButton"
                    tokens: root.tokens
                    text: "导出映射"
                    onClicked: Bridge.exportMappingFile()
                }

                CompactButton {
                    objectName: "importPresetsButton"
                    tokens: root.tokens
                    text: "导入映射"
                    onClicked: importDialog.open()
                }
            }
        }
    }

    FileDialog {
        id: importDialog
        objectName: "importMappingDialog"
        title: "选择要导入的映射文件"
        fileMode: FileDialog.OpenFile
        nameFilters: ["按键映射 (*.json)", "所有文件 (*)"]
        onAccepted: Bridge.importMappingFile(selectedFile)
    }

    readonly property int cardColumnWidth: Math.max(
        180, Math.floor((mappingArea.width - root.mappingPhotoColumnWidth
                         - root.mappingBoardGap * 2) / 2))

    // ==================================================== 动作编辑器
    function openEditor(buttonId, trigger) {
        const row = root.rowOf(buttonId)
        if (!row || row.is_voice)
            return
        actionEditor.buttonId = buttonId
        actionEditor.buttonName = row.button_name
        actionEditor.primaryText = row.single || ""
        actionEditor.doubleText = row.double || ""
        actionEditor.longText = row.long || ""
        actionEditor.primaryNote = row.single_note || ""
        actionEditor.open()
    }

    Dialog {
        id: actionEditor
        objectName: "actionEditorDialog"
        property string buttonId: ""
        property string buttonName: ""
        property string primaryText: ""
        property string doubleText: ""
        property string longText: ""
        property string primaryNote: ""
        property bool syncing: false

        modal: true
        anchors.centerIn: parent
        width: Math.min(430, root.width - 20)
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: (buttonName.length > 0 ? "编辑：" + buttonName : "编辑")

        onAccepted: {
            const row = root.rowOf(actionEditor.buttonId)
            if (!row)
                return
            /* ★ 读框里的文字，不读 actionEditor 上的副本 ★
               下拉框是可编辑的：用户可以直接在里面打 ctrl+shift+p，
               那段字只在 editText 里。以前只存 actionEditor 上的副本，
               手输的组合键按 OK 就丢了。 */
            Bridge.setMapping(row.key_id, "single_click", primaryCombo.editText)
            Bridge.setMapping(row.key_id, "double_click", doubleCombo.editText)
            Bridge.setMapping(row.key_id, "long_press", longCombo.editText)
            Bridge.setMappingNote(row.key_id, actionEditor.primaryNote)
        }

        onOpened: {
            primaryCombo.setEditorValue(actionEditor.primaryText)
            doubleCombo.setEditorValue(actionEditor.doubleText)
            longCombo.setEditorValue(actionEditor.longText)
            primaryNoteField.text = actionEditor.primaryNote
        }

        /*
         * ★ 录完快捷键后要把它显示回框里 ★
         *   以前只在 onOpened 里填一次，弹窗已经开着的时候录完，
         *   框里还是空的 —— 明明录到了，按 OK 也存得进去，
         *   但用户看着像没录上。
         */
        onPrimaryTextChanged: primaryCombo.setEditorValue(actionEditor.primaryText)
        onDoubleTextChanged: doubleCombo.setEditorValue(actionEditor.doubleText)
        onLongTextChanged: longCombo.setEditorValue(actionEditor.longText)

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall
            width: 400

            GridLayout {
                columns: 3
                columnSpacing: root.tokens.spacingMedium
                rowSpacing: root.tokens.spacingSmall

                Item { Layout.preferredWidth: 34 }

                UiLabel {
                    tokens: root.tokens
                    kind: 1
                    text: "按键录入"
                }

                UiLabel {
                    tokens: root.tokens
                    kind: 1
                    text: "备注名称"
                }

                // ---- 单击
                UiLabel {
                    objectName: "actionEditorPrimaryTitle"
                    tokens: root.tokens
                    kind: 0
                    Layout.preferredWidth: 34
                    font.weight: Font.Medium
                    text: "单击"
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
                        onActivated: {
                            if (index >= 0)
                                actionEditor.primaryText = String(model[index])
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorPrimaryRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: "录入"
                        onClicked: root.openRecorder(actionEditor.buttonId,
                                                   "single_click")
                    }
                }
                CompactTextField {
                    id: primaryNoteField
                    objectName: "actionEditorPrimaryNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: "如：复制"
                    onTextChanged: actionEditor.primaryNote = text
                }

                // ---- 双击
                UiLabel {
                    objectName: "actionEditorDoubleTitle"
                    tokens: root.tokens
                    kind: 0
                    Layout.preferredWidth: 34
                    font.weight: Font.Medium
                    text: "双击"
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
                        onActivated: {
                            if (index >= 0)
                                actionEditor.doubleText = String(model[index])
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorDoubleRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: "录入"
                        onClicked: root.openRecorder(actionEditor.buttonId,
                                                   "double_click")
                    }
                }
                Item { Layout.preferredWidth: 126 }

                // ---- 长按
                UiLabel {
                    objectName: "actionEditorLongTitle"
                    tokens: root.tokens
                    kind: 0
                    Layout.preferredWidth: 34
                    font.weight: Font.Medium
                    text: "长按"
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
                        onActivated: {
                            if (index >= 0)
                                actionEditor.longText = String(model[index])
                        }
                    }
                    CompactButton {
                        objectName: "actionEditorLongRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth2Chars
                        text: "录入"
                        onClicked: root.openRecorder(actionEditor.buttonId,
                                                   "long_press")
                    }
                }
                Item { Layout.preferredWidth: 126 }
            }

            UiLabel {
                tokens: root.tokens
                kind: 1
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "双击会等待约 0.3 秒区分单击和双击；"
                      + "长按按住约 0.55 秒触发。"
            }
        }
    }

    // ==================================================== 快捷键录制
    function openRecorder(buttonId, trigger) {
        recorder.buttonId = buttonId
        recorder.trigger = trigger
        recorder.inputMode = "capture"
        recorder.previewText = "请按下希望遥控器发送的键盘快捷键"
        recorder.manualErrorText = ""
        recorder.open()
    }

    Dialog {
        id: recorder
        objectName: "shortcutRecorderDialog"
        property string buttonId: ""
        property string trigger: "single_click"
        property string previewText: ""
        property string manualErrorText: ""
        property string inputMode: "capture"

        modal: true
        anchors.centerIn: parent
        width: Math.min(430, root.width - 20)
        standardButtons: Dialog.NoButton
        title: "录入快捷键"

        onOpened: {
            Bridge.startHotkeyCapture()
        }

        /* ★ 关闭时必须停掉录制 ★
           钩子一直挂着会吞掉全系统键盘输入。 */
        onClosed: {
            Bridge.stopHotkeyCapture()
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingMedium
            width: 400

            RowLayout {
                spacing: 0
                CompactButton {
                    objectName: "recorderCaptureModeButton"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    text: "按键录入"
                    highlighted: recorder.inputMode === "capture"
                    onClicked: {
                        recorder.inputMode = "capture"
                        recorder.manualErrorText = ""
                        if (!Bridge.hotkeyCapturing)
                            Bridge.startHotkeyCapture()
                    }
                }
                CompactButton {
                    objectName: "recorderManualModeButton"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    text: "手动输入"
                    highlighted: recorder.inputMode === "manual"
                    onClicked: {
                        if (Bridge.hotkeyCapturing)
                            Bridge.stopHotkeyCapture()
                        recorder.inputMode = "manual"
                        recorder.manualErrorText = ""
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                visible: recorder.inputMode === "capture"
                spacing: root.tokens.spacingSmall

                UiLabel {
                    tokens: root.tokens
                    kind: 0
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: recorder.previewText
                    color: tokens.accent
                }
                UiLabel {
                    tokens: root.tokens
                    kind: 1
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: "按下要发送的单键或组合键"
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                visible: recorder.inputMode === "manual"
                spacing: root.tokens.spacingSmall

                CompactTextField {
                    id: manualField
                    objectName: "recorderManualField"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    placeholderText: "例如 Ctrl+[、Win+L 或 左 Ctrl+←"
                    onAccepted: recorder.commitManual()
                }
                UiLabel {
                    tokens: root.tokens
                    kind: 1
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: recorder.manualErrorText.length > 0
                          ? recorder.manualErrorText
                          : "可直接修改键名；用 + 连接"
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "recorderCancelButton"
                    tokens: root.tokens
                    text: "取消"
                    onClicked: recorder.close()
                }
                CompactButton {
                    objectName: "recorderConfirmButton"
                    tokens: root.tokens
                    text: "确定"
                    highlighted: true
                    visible: recorder.inputMode === "manual"
                    enabled: manualField.text.trim().length > 0
                    onClicked: recorder.commitManual()
                }
            }
        }

        function commitManual() {
            const text = manualField.text.trim()
            if (text.length === 0)
                return
            root.applyCaptured(text)
            recorder.close()
        }
    }

    function applyCaptured(chord) {
        const row = root.rowOf(recorder.buttonId)
        if (!row)
            return
        if (recorder.trigger === "single_click")
            actionEditor.primaryText = chord
        else if (recorder.trigger === "double_click")
            actionEditor.doubleText = chord
        else
            actionEditor.longText = chord
    }

    Connections {
        target: Bridge
        function onHotkeyCaptureChanged() {
            if (!Bridge.hotkeyCapturing && Bridge.capturedHotkey.length > 0
                    && recorder.visible && recorder.inputMode === "capture") {
                recorder.previewText = Bridge.capturedHotkey
                root.applyCaptured(Bridge.capturedHotkey)
                recorder.close()
            }
        }
    }

    // ==================================================== 恢复默认
    Dialog {
        id: restoreDialog
        objectName: "restoreMappingDefaultsDialog"
        modal: true
        anchors.centerIn: parent
        width: 360
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: "恢复内置默认？"
        onAccepted: Bridge.restoreMappingDefaults()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: 0
            width: 320
            wrapMode: Text.WordWrap
            text: "这会把当前遥控器的 15 个按键恢复为内置映射，"
                  + "并立即写回开发板。"
        }
    }
}
