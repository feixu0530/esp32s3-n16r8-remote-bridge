/*
 * DevicePage.qml - 设备页
 * ---------------------------------------------------------------------------
 * 照抄 windows-remote-mic-app 的 DevicePage.qml 结构：
 *   设备状态 / 通用设置 / 固件 / 蓝牙
 *
 * 与参考项目的差别：
 *   - 「蓝牙设置」按钮换成了**开发板自己的**蓝牙配对
 *     （参考项目用的是 Windows 的蓝牙面板，我们用的是板子的 BLE）
 *   - 多了固件版本、开发板插拔检测
 *   - 诊断日志是**板端滚动日志**：断电重插前的历史也能拉回来
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Item {
    id: root
    property var tokens: Tokens {}
    property var firmwareFiles: []
    /* ★ 烧录输出框平时折叠 ★
       点了烧录才展开，把竖向空间让给下面的诊断日志。 */
    property bool flashLogExpanded: false

    readonly property int stateColumnWidth: 110
    readonly property int actionColumnWidth: tokens.buttonWidth6Chars

    /* 串口下拉：显示「端口 — 角色」，不要把 VID/PID 全塞进正文 */
    readonly property var portEntries: {
        var out = []
        var list = Bridge.portList
        for (var i = 0; i < list.length; i++) {
            out.push({
                device: list[i].device,
                role: list[i].protocol ? "协议通道" : "烧录通道",
                protocol: list[i].protocol,
                text: list[i].device + " — "
                      + (list[i].protocol ? "协议通道" : "烧录通道")
            })
        }
        return out
    }
    property string selectedPort: ""

    function portDescription() {
        var list = Bridge.portList
        if (list.length === 0)
            return "没找到开发板 —— 用 USB 线插上 ESP32-S3"
        var n = 0
        for (var i = 0; i < list.length; i++) {
            if (list[i].protocol)
                n++
        }
        return n > 0 ? "协议通道用来收发命令，烧录通道用来刷固件"
                     : "没有找到协议通道 —— 检查开发板的 USB 线"
    }

    function currentPortLabel() {
        for (var i = 0; i < Bridge.portList.length; i++) {
            if (Bridge.portList[i].device === Bridge.devicePort)
                return Bridge.portList[i].device
        }
        // 没连上时退回第一个**协议通道**
        // （以前这里写的是 root.portLabels —— 那个属性根本不存在，
        //   一走到这条分支就报 "Cannot read property 'length' of undefined"）
        var ports = Bridge.protocolPorts
        return (ports && ports.length > 0) ? ports[0].device : ""
    }

    function connectedRemoteCount() {
        var n = 0
        for (var i = 0; i < RemoteListModel.count; i++) {
            if (RemoteListModel.get(i).connected)
                n++
        }
        return n
    }

    function boardStateText() {
        if (!Bridge.devicePresent)
            return "未插入"
        /* ★ 口在 ≠ 通道通 ★
           v0.14.x 这里写的是「已插入」，用户以为一切正常，
           其实协议通道根本没建，命令一条都没发出去。 */
        return Bridge.connected ? "已连接" : "未连接"
    }

    function boardStateColor() {
        if (!Bridge.devicePresent)
            return tokens.errorColor
        return Bridge.connected ? tokens.successColor : tokens.voiceAccent
    }

    /* 手机端（临时遥控器）的联网状态在「手机」页，这里只管设备页的事 */

    onVisibleChanged: {
        if (visible) {
            Bridge.refreshDeviceInfo()
            Bridge.refreshVoiceKey()
            /* 进页面就把板子的联网状态 + 记住的 WiFi 拉回来，
               不用用户先点一次「刷新」才看得到配网区块的内容 */
            if (Bridge.connected) {
                Bridge.refreshWifi()
                Bridge.refreshWifiNets()
            }
        }
    }

    /* 板子是后插上的也要自动填好配网区块 */
    Connections {
        target: Bridge
        function onConnectedChanged() {
            if (Bridge.connected) {
                Bridge.refreshWifi()
                Bridge.refreshWifiNets()
            }
        }
    }

    ScrollView {
        id: scroll
        objectName: "deviceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: content
            objectName: "devicePageContent"
            width: Math.max(0, scroll.availableWidth - root.tokens.pageHorizontalPadding * 2)
            x: root.tokens.pageHorizontalPadding
            y: root.tokens.pageVerticalPadding
            spacing: root.tokens.spacingMedium

            // ============================================== 开发板连接

            // ============================================== 设备状态
            SectionFrame {
                objectName: "deviceStatusSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "deviceStatusSectionTitle"
                    text: "设备状态"
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "boardRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "开发板"
                    // 端口号左边下拉已经显示了，这里不再重复（以前写 COM12）
                    descriptionText: Bridge.devicePort.length > 0
                        ? "协议通道（板载原生 USB）"
                        : "插上开发板后这里会显示串口"
                    stateText: root.boardStateText()
                    stateColor: root.boardStateColor()

                    /*
                     * ★ 串口下拉搬到这里 ★
                     *   以前「开发板连接」区和「设备状态」区各说各的，
                     *   用户要在一个地方选开发板。
                     *   ★ 只列协议通道 ★ —— 烧录通道平时不用插，
                     *   混进来只会让人选错。
                     */
                    editorData: [SelectionComboBox {
                        id: portCombo
                        objectName: "portCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.minimumWidth: 170
                        model: Bridge.protocolPorts
                        textRole: "text"
                        enabled: !Bridge.connected
                        currentIndex: Math.max(0, Bridge.protocolPorts.map(
                            function (e) { return e.device })
                            .indexOf(root.currentPortLabel()))
                        onActivated: function (index) {
                            root.selectedPort = Bridge.protocolPorts[index].device
                            Bridge.connectPort(root.selectedPort)
                        }
                        Accessible.name: "协议通道"
                    }]

                    CompactButton {
                        objectName: "connectButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: Bridge.connected ? "断开" : "连接"
                        highlighted: !Bridge.connected
                        enabled: Bridge.connected || Bridge.protocolPorts.length > 0
                        onClicked: {
                            if (Bridge.connected) {
                                Bridge.disconnectDevice()
                            } else {
                                var p = root.selectedPort.length > 0
                                        ? root.selectedPort : root.currentPortLabel()
                                Bridge.connectPort(p)
                            }
                        }
                    }
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "firmwareVersionRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "固件版本"
                    descriptionText: Bridge.firmwareVersion.length > 0
                        ? "板子上正在运行的固件" : "连上开发板后自动读取"
                    stateText: Bridge.firmwareVersion.length > 0
                        ? Bridge.firmwareVersion : "未知"
                    stateColor: Bridge.firmwareVersion.length > 0
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "refreshDeviceButton"
                        enabled: Bridge.connected
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        onClicked: Bridge.refreshDeviceInfo()
                    }
                }

                /*
                 * ★ 刷写固件 ★
                 *   选烧录通道 → 选固件 → 烧录。
                 *   烧录 = 整个固件上传刷写（全新刷写，会清 NVS），
                 *   刷完自动重连，并按 MAC 归档把按键映射恢复回去。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "flashRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "刷写固件"
                    // 这里原来写「烧录通道：COM6」——左边下拉已经显示了，重复；
                    // 改成显示**选中的固件名**，一眼能看出有没有选。
                    descriptionText: {
                        var files = root.firmwareFiles
                        if (!files || files.length === 0)
                            return "还没选固件（将使用包内固件）"
                        var names = []
                        for (var i = 0; i < files.length; i++) {
                            var p = (typeof files[i] === "object" && files[i] !== null)
                                    ? files[i].path : String(files[i])
                            names.push(String(p).split(/[\\/]/).pop())
                        }
                        return "已选：" + names.join("、")
                    }
                    descriptionNeverElide: true
                    stateText: Bridge.flashBusy
                        ? "刷写中 " + Math.round(Bridge.flashProgress) + "%"
                        : (Bridge.flashPorts.length > 0 ? "就绪" : "无烧录通道")
                    stateColor: Bridge.flashBusy
                        ? tokens.accent
                        : (Bridge.flashPorts.length > 0
                            ? tokens.successColor : tokens.errorColor)

                    editorData: [
                        SelectionComboBox {
                            id: flashCombo
                            objectName: "flashPortCombo"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            Layout.minimumWidth: 150
                            model: Bridge.flashPorts
                            textRole: "text"
                            enabled: !Bridge.flashBusy
                            currentIndex: Math.max(0, Bridge.flashPorts.map(
                                function (e) { return e.device })
                                .indexOf(Bridge.flashPort))
                            onActivated: function (index) {
                                Bridge.setFlashPort(Bridge.flashPorts[index].device)
                            }
                            Accessible.name: "烧录通道"
                        },
                        CompactButton {
                            objectName: "pickFirmwareButton"
                            tokens: root.tokens
                            compactMinimumWidth: root.tokens.buttonWidth6Chars
                            text: "选固件"
                            enabled: !Bridge.flashBusy
                            onClicked: firmwareDialog.open()
                        }
                    ]

                    /*
                     * ★ 两个刷写按钮并排、同色 ★
                     *   「仅应用」也是刷写，以前藏在「选固件」旁边、样式又不同，
                     *   很容易被当成「选一个只烧应用区的固件」。
                     */
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: root.tokens.spacingSmall
                        CompactButton {
                            objectName: "flashAppButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth6Chars
                            text: "仅应用"
                            highlighted: true
                            enabled: !Bridge.flashBusy
                            onClicked: {
                                root.flashLogExpanded = true
                                Bridge.flashFirmwareAppOnly(root.firmwareFiles)
                            }
                        }
                        CompactButton {
                            objectName: "flashButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth4Chars
                            text: "烧录"
                            highlighted: true
                            enabled: !Bridge.flashBusy && Bridge.flashPorts.length > 0
                            onClicked: {
                                root.flashLogExpanded = true
                                fullFlashDialog.open()
                            }
                        }
                    }
                }

                /* 刷写进度：就在刷写按钮下面，不用滚到页面最底下找 */
                /* 刷写输出：平时折叠，点了烧录才展开 */
                RowLayout {
                    objectName: "flashLogHeader"
                    Layout.fillWidth: true
                    Layout.leftMargin: root.tokens.spacingLarge
                    Layout.rightMargin: root.tokens.spacingLarge
                    Layout.topMargin: root.tokens.spacingSmall
                    visible: Bridge.flashLog.length > 0 || root.flashLogExpanded

                    CompactButton {
                        objectName: "flashLogToggle"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: root.flashLogExpanded ? "收起烧录输出" : "展开烧录输出"
                        onClicked: root.flashLogExpanded = !root.flashLogExpanded
                    }
                    Item { Layout.fillWidth: true }
                }

                Rectangle {
                    objectName: "flashLogPanel"
                    Layout.fillWidth: true
                    /* ★ 折叠时完全不占空间 ★ */
                    Layout.preferredHeight: root.flashLogExpanded ? 110 : 0
                    Layout.minimumHeight: 0
                    visible: root.flashLogExpanded
                    Layout.leftMargin: root.tokens.spacingLarge
                    Layout.rightMargin: root.tokens.spacingLarge
                    Layout.bottomMargin: root.tokens.spacingSmall
                    radius: root.tokens.cornerRadiusSmall
                    color: root.tokens.fieldBackground
                    border.color: root.tokens.border
                    border.width: root.tokens.hairlineWidth
                    clip: true

                    ListView {
                        id: flashLogList
                        objectName: "flashLogList"
                        anchors.fill: parent
                        anchors.margins: 6
                        clip: true
                        model: Bridge.flashLog.length > 0 ? Bridge.flashLog.split("\n") : []
                        boundsBehavior: Flickable.StopAtBounds
                        onCountChanged: positionViewAtEnd()

                        delegate: Text {
                            required property string modelData
                            color: root.tokens.textSecondary
                            font.family: root.tokens.fontFamilyMono
                            font.pixelSize: root.tokens.fontSizeSmall
                            wrapMode: Text.NoWrap
                            elide: Text.ElideRight
                            width: flashLogList.width
                            text: modelData
                        }
                    }
                }

            }
            SectionFrame {
                objectName: "bluetoothSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "bluetoothSectionTitle"
                    text: "蓝牙配对"
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "pairModeRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "配对模式"
                    descriptionText: Bridge.pairWindowRemaining > 0
                        ? "请把遥控器长按 Home 进配对模式，开发板会自动配上"
                        : (Bridge.pairWindowAuto
                            ? "开发板一个遥控器都没配，正在自动等人来配对 —— "
                              + "把遥控器长按 Home 进配对模式，它会自己配上"
                            : "遥控器只有在自己的配对模式下才会广播。"
                              + "点右边开窗后，两边同时进配对就能配上")
                    stateText: Bridge.pairWindowRemaining > 0
                        ? "配对窗口 " + Bridge.pairWindowRemaining + " 秒"
                        : (Bridge.pairWindowAuto ? "等待遥控器" : "已关闭")
                    stateColor: (Bridge.pairWindowAuto
                                 || Bridge.pairWindowRemaining > 0)
                        ? tokens.voiceAccent : tokens.disabledText

                    CompactButton {
                        objectName: "startPairModeButton"
                        enabled: Bridge.connected
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: Bridge.pairWindowRemaining > 0
                              ? "停止配对" : "进入配对模式"
                        highlighted: Bridge.pairWindowRemaining <= 0
                        onClicked: {
                            if (Bridge.pairWindowRemaining > 0)
                                Bridge.stopPairWindow()
                            else
                                Bridge.startPairWindow(120)
                        }
                    }
                }

                // --------------------------------------- 已配对列表
                /*
                 * ★ 已连接 / 已配对 用 1/1 的形式 ★
                 *   前一个 = 现在连着的台数，后一个 = 配对表里的总台数。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "pairedSummaryRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleWidth: 180
                    titleText: "遥控器"
                    descriptionText: "已连接 / 已配对"
                    descriptionNeverElide: true
                    /* ★ 只数蓝牙遥控器 ★
                       手机是 WiFi 设备，它有自己的页面，不算进蓝牙这一栏。 */
                    stateText: RemoteListModel.bleConnectedCount + "/"
                               + RemoteListModel.bleCount
                    stateColor: RemoteListModel.bleConnectedCount > 0
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "refreshRemotesButton"
                        enabled: Bridge.connected
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        onClicked: Bridge.refreshEverything()
                    }
                }

                /*
                 * ★ 扫描做成弹窗（跟 Windows / 安卓的「添加设备」一致）★
                 *
                 *   以前候选列表直接铺在设备页里，扫描开着就一直往下列里塞，
                 *   把整个页面拉得老长 —— 用户看着像「板子一直在扫描，停不下来」。
                 *   现在：点「扫描」→ 弹窗 → 选一台配对 → 弹窗自动关、扫描自动停。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "scanRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleWidth: 180
                    titleText: "附近的遥控器"
                    descriptionText: "点右边「扫描」挑一台配对。"
                                      + "★遥控器只在醒着时广播★ —— 扫着的时候按一下它的任意键"
                    stateText: Bridge.scanDialogOpen ? "扫描中" : ""
                    stateColor: tokens.voiceAccent

                    CompactButton {
                        objectName: "scanToggleButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "扫描"
                        enabled: Bridge.connected
                        onClicked: {
                            scanDialog.open()
                            Bridge.startScan()
                        }
                    }
                }

                Repeater {
                    model: RemoteListModel
                    delegate: InlineSettingsRow {
                        id: row
                        required property int index
                        required property string name
                        required property string addr
                        required property bool connected
                        required property int binds
                        required property int bindsBoard
                        required property string kind

                        /*
                         * ★ 手机不进「蓝牙配对」列表 ★
                         *   手机走 WiFi，不是蓝牙配对来的设备 ——
                         *   列在这儿会让人以为它走蓝牙。它在「手机」页。
                         */
                        visible: row.kind !== "phone"

                        tokens: root.tokens
                        stateColumnWidth: root.stateColumnWidth
                        /*
                         * ★ 名字列要够宽 ★
                         *   默认 72 px 会把 "Chromecast Media Remote" 截成
                         *   "Chromeca…" —— 遥控器名字看不全就没法区分是哪台。
                         *   行里还有约 300 px 弹性余量，加宽不会挤掉右侧按钮。
                         */
                        titleWidth: 180
                        /* 三个按钮（重命名/删除/同步）时 3×92 + 2×6 = 288 */
                        actionColumnWidth: 184
                        titleText: row.name.length > 0
                            ? row.name : "遥控器 " + (row.index + 1)
                        descriptionText: row.addr
                        descriptionNeverElide: true
                        /*
                         * ★ 别只写「未连接」★
                         *   Chromecast 遥控器空闲时**完全不广播**（省电），
                         *   所以「已配对但现在没连着」是常态 ——
                         *   以前界面写「未连接」，用户以为没配上，
                         *   就把配对删了（配对记录一删，那台再也回不来）。
                         *   现在说清楚：还配着呢，按一下遥控器它就醒。
                         */
                        stateText: row.connected
                            ? "已连接"
                            : (RemoteListModel.count > 0
                                ? "已配对 · 遥控器在睡觉"
                                : "未连接")
                        stateColor: row.connected
                            ? tokens.successColor : tokens.disabledText

                        CompactButton {
                            objectName: "renameRemote"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth6Chars
                            text: "重命名"
                            onClicked: {
                                remoteNameDialog.remoteIndex = row.index
                                remoteNameDialog.currentName = row.name
                                remoteNameDialog.open()
                            }
                        }

                        CompactButton {
                            objectName: "removeRemote"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth6Chars
                            text: "删除"
                            /*
                             * ★ 删除是不可逆的 ★
                             *   删的是**板子上的配对记录**（NVS）——
                             *   删完这台就再也回不来了（映射、名字全没）。
                             *   以前一点就删，用户以为只是从列表里移除。
                             */
                            onClicked: {
                                unpairDialog.remoteIndex = row.index
                                unpairDialog.remoteName = row.name
                                unpairDialog.open()
                            }
                        }

                    }
                }

                /*
                 * ★ 手机也是蓝牙设备，跟遥控器列在一起 ★
                 *   放在已配对遥控器下方、用「手机 App」分类区分，
                 *   一眼能看到「板子当前连了哪些蓝牙设备」。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "phoneInfoRow"
                    stateColumnWidth: root.stateColumnWidth
                    /* 和遥控器行同宽，两个按钮才不会被挤成「重..」 */
                    titleWidth: 180
                    actionColumnWidth: 184
                    titleText: Bridge.phoneDisplayName
                    descriptionText: "手机 App · "
                        + (Bridge.phoneIdAddr.length > 0
                            ? Bridge.phoneIdAddr
                            : (Bridge.phoneAddr.length > 0
                                ? Bridge.phoneAddr
                                : "连上后显示地址"))
                    descriptionNeverElide: true
                    stateText: Bridge.phoneOnline ? "已连接" : "未连接"
                    stateColor: Bridge.phoneOnline
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "renamePhone"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "重命名"
                        onClicked: {
                            phoneNameDialog.currentName = Bridge.phoneDisplayName
                            phoneNameDialog.open()
                        }
                    }

                    CompactButton {
                        objectName: "removePhone"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "删除"
                        onClicked: phoneDeleteDialog.open()
                    }
                }

                Item { Layout.preferredHeight: root.tokens.pageVerticalPadding }
            }
            SectionFrame {
                objectName: "generalSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "generalSectionTitle"
                    text: "通用设置"
                }

                SettingsListRow {
                    tokens: root.tokens
                    objectName: "launchAtLoginRow"
                    
                    titleText: "随 Windows 启动"
                    descriptionText: "登录后后台运行"

                    CompactSwitch {
                        id: launchAtLoginSwitch
                        objectName: "launchAtLoginSwitch"
                        tokens: root.tokens
                        checked: Bridge.launchAtLogin
                        Accessible.name: "随 Windows 启动"
                        onToggled: {
                            if (checked !== Bridge.launchAtLogin)
                                Bridge.setLaunchAtLogin(checked)
                        }
                    }
                }

                SettingsListRow {
                    tokens: root.tokens
                    objectName: "closeBehaviorRow"
                    
                    titleText: "关闭窗口时隐藏到任务栏"
                    descriptionText: "保留右下角图标；关闭此项则完全退出"

                    CompactSwitch {
                        id: closeBehaviorSwitch
                        objectName: "closeBehaviorSwitch"
                        tokens: root.tokens
                        checked: Bridge.closeBehavior !== "quit"
                        Accessible.name: "关闭窗口时隐藏到任务栏"
                        onToggled: {
                            if (checked !== (Bridge.closeBehavior !== "quit"))
                                Bridge.setCloseBehavior(checked ? "hide" : "quit")
                        }
                    }
                }

                /*
                 * ★ 按键自检（从语音页挪过来）★
                 *   点一下板子造一次假按键（灯闪），下面日志里会多一行 ——
                 *   放在日志上方就是为了「点完立刻看得见」。
                 */
                SettingsListRow {
                    tokens: root.tokens
                    objectName: "keySelfTestRow"
                    titleText: "按键自检"
                    descriptionText: "让开发板造一个假按键事件：板端灯闪一下，下面日志会多一行"

                    CompactButton {
                        objectName: "keySelfTestButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "发一次"
                        enabled: Bridge.connected
                        onClicked: Bridge.selfTest()
                    }
                }

                SettingsListRow {
                    tokens: root.tokens
                    objectName: "diagnosticLogRow"
                    titleText: "诊断日志"
                    descriptionText: "导出会存到「下载」文件夹"

                    CompactLink {
                        tokens: root.tokens
                        objectName: "openLogFolderLink"
                        text: "打开日志位置"
                        onClicked: Bridge.openLogFolder()
                    }

                    CompactLink {
                        tokens: root.tokens
                        objectName: "exportLogLink"
                        text: "导出日志"
                        onClicked: Bridge.exportLogs()
                    }

                }

                // --------------------------------------- 实时日志面板
                Rectangle {
                    objectName: "logPanel"
                    Layout.fillWidth: true
                    /* 固定高度：页面不再整体拉伸，
                       段落之间不会被撑出空白 */
                    Layout.preferredHeight: 160
                    Layout.leftMargin: root.tokens.spacingLarge
                    Layout.rightMargin: root.tokens.spacingLarge
                    Layout.bottomMargin: root.tokens.spacingSmall
                    radius: root.tokens.cornerRadiusSmall
                    color: root.tokens.fieldBackground
                    border.color: root.tokens.border
                    border.width: root.tokens.hairlineWidth
                    clip: true

                    ListView {
                        id: logList
                        objectName: "logList"
                        anchors.fill: parent
                        anchors.margins: 6
                        clip: true
                        model: Bridge.logLines
                        boundsBehavior: Flickable.StopAtBounds
                        onCountChanged: positionViewAtEnd()

                        delegate: Text {
                            required property string modelData
                            color: root.tokens.textSecondary
                            font.family: root.tokens.fontFamilyMono
                            font.pixelSize: root.tokens.fontSizeSmall
                            wrapMode: Text.NoWrap
                            elide: Text.ElideRight
                            width: logList.width
                            text: modelData
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        visible: logList.count === 0
                        text: "还没有日志。按一下遥控器，或点上面的「按键自检」，"
                              + "这里会实时显示按键与映射记录。"
                        color: root.tokens.disabledText
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeBody
                        horizontalAlignment: Text.AlignHCenter
                        width: parent.width - 40
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }

    // ------------------------------------------------------ 对话框
    FileDialog {
        id: firmwareDialog
        title: "选择固件文件"
        fileMode: FileDialog.OpenFiles
        nameFilters: ["固件 (*.bin)", "所有文件 (*)"]
        onAccepted: root.firmwareFiles = Bridge.pickFirmwareFiles(selectedFiles)
    }

    // ------------------------------------------------------ 扫描弹窗
    Dialog {
        id: scanDialog
        objectName: "scanDialog"
        title: "扫描附近的遥控器"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: 460
        height: 420
        onOpened: Bridge.setScanDialogOpen(true)
        onClosed: {
            /* 关窗就把扫描停掉 —— 别让板子一直扫 */
            Bridge.setScanDialogOpen(false)
            Bridge.stopScan()
            Bridge.closePairWindow()
        }

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.color: root.tokens.border
            border.width: root.tokens.hairlineWidth
        }
        header: Item { height: 0 }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall

            Text {
                text: "遥控器只在醒着（被按一下）的时候广播。"
                      + "扫不出来就先按一下它的键。"
                color: root.tokens.textSecondary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            ListView {
                id: scanList
                objectName: "scanList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: ScanModel
                spacing: 4
                delegate: Item {
                    id: cand
                    required property int index
                    required property string name
                    required property string addr
                    required property int rssi
                    required property int addrType

                    objectName: "scanCandidateRow"
                    /*
                     * ★ ListView 的 delegate 必须自己量宽度/高度 ★
                     *   这里用 Layout.fillWidth / implicitHeight 是**无效**的
                     *   （那是给 Layout 用的），结果高度算成 0、
                     *   名字和地址叠在一起 —— 就是「信息挤成一团」。
                     */
                    width: ListView.view ? ListView.view.width : 0
                    height: 56

                    Rectangle {
                        anchors.fill: parent
                        radius: root.tokens.cornerRadiusSmall
                        color: root.tokens.surfaceMuted
                        border.color: root.tokens.border
                        border.width: root.tokens.hairlineWidth
                    }

                    Text {
                        id: candName
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.right: candBtn.left
                        anchors.rightMargin: 8
                        anchors.top: parent.top
                        anchors.topMargin: 8
                        text: cand.name.length > 0 ? cand.name : "未命名设备"
                        color: root.tokens.textPrimary
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeBody
                        font.weight: Font.Medium
                        elide: Text.ElideRight
                    }

                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.right: candBtn.left
                        anchors.rightMargin: 8
                        anchors.top: candName.bottom
                        anchors.topMargin: 4
                        text: cand.addr + "  ·  " + cand.rssi + " dBm"
                        color: root.tokens.textSecondary
                        font.family: root.tokens.fontFamily
                        font.pixelSize: root.tokens.fontSizeSmall
                        elide: Text.ElideMiddle
                    }

                    CompactButton {
                        id: candBtn
                        objectName: "pairCandidateButton"
                        anchors.right: parent.right
                        anchors.rightMargin: 8
                        anchors.verticalCenter: parent.verticalCenter
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "配对"
                        enabled: Bridge.connected
                        onClicked: {
                            /*
                             * ★ 点「配对」时**不能停扫描** ★
                             *   板子要靠扫描抓到这台遥控器的**新鲜广播**
                             *   才能连上（遥控器的广播地址会轮换）。
                             *   以前这里立刻关窗 → 关窗会停扫描 →
                             *   板子只能拿一个旧地址死连 → 「点了没反应」。
                             *   这里只收窗，扫描继续，配对结果看状态栏。
                             */
                            Bridge.setScanDialogOpen(false)
                            scanDialog.visible = false
                            Bridge.pairCandidate(cand.addr, cand.addrType, cand.name)
                        }
                    }
                }
            }

            Text {
                objectName: "scanEmptyText"
                visible: ScanModel.count === 0
                text: "正在扫…还没扫到设备。"
                      + "★遥控器只在醒着时广播★ —— 现在按一下它的任意键。"
                color: root.tokens.textSecondary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }
        }
    }

    // ------------------------------------------------------ 删除确认弹窗
    Dialog {
        id: unpairDialog
        objectName: "unpairDialog"
        property int remoteIndex: -1
        property string remoteName: ""

        title: "删除这台遥控器？"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: 400
        standardButtons: Dialog.Yes | Dialog.No

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.color: root.tokens.border
            border.width: root.tokens.hairlineWidth
        }
        header: Item { height: 0 }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall

            Text {
                text: "删掉的是**开发板上这台遥控器的配对记录**："
                      + "映射、名字都会没，删完它就回不来了"
                      + "（要再配上得重新扫描配对）。"
                color: root.tokens.textSecondary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            Text {
                text: unpairDialog.remoteName
                color: root.tokens.textPrimary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                font.weight: Font.Medium
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
        }

        onAccepted: {
            if (unpairDialog.remoteIndex >= 0) {
                Bridge.removeRemote(unpairDialog.remoteIndex)
            }
        }
    }

    // ------------------------------------------------------ 重命名弹窗
    Dialog {
        id: remoteNameDialog
        objectName: "remoteNameDialog"
        property int remoteIndex: -1
        property string currentName: ""

        title: "重命名遥控器"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: 360
        standardButtons: Dialog.Ok | Dialog.Cancel

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.color: root.tokens.border
            border.width: root.tokens.hairlineWidth
        }
        header: Item { height: 0 }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall

            Text {
                text: "同型号的遥控器广播名是一样的（Chromecast 全是 HID_Device），"
                      + "起个能分清的名字。"
                color: root.tokens.textSecondary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            CompactTextField {
                id: remoteNameField
                objectName: "remoteNameField"
                tokens: root.tokens
                Layout.fillWidth: true
                text: remoteNameDialog.currentName
                placeholderText: "遥控器名字"
            }
        }

        onAccepted: {
            Bridge.setRemoteName(remoteNameDialog.remoteIndex,
                                 remoteNameField.text)
        }
    }

    /* ---- 手机改名 / 删除（多台手机、发给别人用时要能区分和重配） ---- */
    SettingsDialog {
        id: phoneNameDialog
        tokens: root.tokens
        objectName: "phoneNameDialog"
        property string currentName: ""

        title: "重命名手机"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: 360
        standardButtons: Dialog.Ok | Dialog.Cancel

        background: Rectangle {
            radius: root.tokens.cornerRadiusLarge
            color: root.tokens.surface
            border.color: root.tokens.border
            border.width: root.tokens.hairlineWidth
        }

        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall

            Text {
                text: "手机连上后会自动报出机型名（如 Xiaomi 23013RK75C）。"
                      + "多台手机或发给别人用时，起个能分清的别名。"
                color: root.tokens.textSecondary
                font.family: root.tokens.fontFamily
                font.pixelSize: root.tokens.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            CompactTextField {
                id: phoneNameField
                objectName: "phoneNameField"
                tokens: root.tokens
                Layout.fillWidth: true
                text: phoneNameDialog.currentName
                placeholderText: "手机名字（留空恢复机型名）"
            }
        }

        onAccepted: Bridge.renamePhone(phoneNameField.text)
    }

    SettingsDialog {
        id: phoneDeleteDialog
        tokens: root.tokens
        objectName: "phoneDeleteDialog"
        title: "断开这台手机？"
        preferredWidth: 420
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: Bridge.deletePhone()

        contentItem: Text {
            color: root.tokens.textPrimary
            font.family: root.tokens.fontFamily
            font.pixelSize: root.tokens.fontSizeBody
            wrapMode: Text.WordWrap
            text: "会把开发板与这台手机的蓝牙连接断开，并清掉你给它起的名字。

"
                  + "手机没有「板子侧配对记录」：装了 App 的手机随时可以再连上来，"
                  + "重新连接后可以再改名。"
        }
    }

    SettingsDialog {
        id: fullFlashDialog
        tokens: root.tokens
        objectName: "fullFlashDialog"
        title: "全新刷写？"
        preferredWidth: 420
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: Bridge.flashFirmware([])
        contentItem: Text {
            color: root.tokens.textPrimary
            font.family: root.tokens.fontFamily
            font.pixelSize: root.tokens.fontSizeBody
            wrapMode: Text.WordWrap
            text: "全新刷写会先擦掉整片 flash，"
                  + "遥控器的配对记录、按键映射、语音快捷键都会被清空。\n\n"
                  + "如果只是升级版本，请用「仅更新应用」。"
        }
    }
}
