/*
 * PhonePage.qml - 手机端（临时遥控器）页
 * ---------------------------------------------------------------------------
 * 手机是**通过 WiFi** 临时顶替遥控器的，不是蓝牙配对来的设备。
 * 所以它单独一页：
 *   - 手机地址（手机上填哪个）
 *   - 手机现在的连接状态
 *   - 开发板配网 / 记住的 WiFi（配网在 PC 端做，板子插在电脑上最直接）
 *
 * ★ 为什么从设备页挪出来 ★
 *   手机出现在「蓝牙配对」列表里，用户会以为它走了蓝牙 ——
 *   实际上它走的是 WiFi，地址、状态、配网都跟蓝牙那一套没关系。
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var tokens: Tokens {}

    readonly property int stateColumnWidth: 110
    readonly property int actionColumnWidth: tokens.buttonWidth6Chars

    /* 板子的联网状态 —— 人话，不要暴露 sta/ap 这种内部词 */
    function wifiStateText() {
        if (Bridge.wifiState === "sta")
            return "已连 " + Bridge.wifiSsid + "（" + Bridge.wifiIp + "）"
        if (Bridge.wifiState === "ap")
            return "热点模式（手机连 ESP32-Remote 热点）"
        return "未连接"
    }

    function wifiStateColor() {
        if (Bridge.wifiState === "sta")
            return tokens.successColor
        if (Bridge.wifiState === "ap")
            return tokens.voiceAccent
        return tokens.disabledText
    }

    /* 手机在线状态：板子按「最近有没有发过心跳」报的 */
    function phoneStateText() {
        if (!Bridge.connected)
            return "开发板未连接"
        return Bridge.phoneOnline ? "已连接（WiFi）" : "未连接"
    }

    function phoneStateColor() {
        if (!Bridge.connected)
            return tokens.disabledText
        return Bridge.phoneOnline ? tokens.successColor : tokens.disabledText
    }

    onVisibleChanged: {
        if (visible) {
            Bridge.refreshWifi()
            Bridge.refreshWifiNets()
            Bridge.refreshRemotes()
        }
    }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: col.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: col
            width: parent.width
            spacing: 0

            /*
             * ============================================== 手机端（临时遥控器）
             *
             * 没有 Chromecast 遥控器时，手机临时顶替：
             *   手机 → WiFi → 开发板 → USB HID → PC。
             */
            SectionFrame {
                objectName: "phoneSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "phoneSectionTitle"
                    text: "手机端（临时遥控器）"
                }

                /*
                 * ★ 手机地址放这一区最上面 ★
                 *   用户第一眼要的就是「手机上填哪个地址」——
                 *   填错就出现「连不上电脑」，而板子的 IP 是 DHCP 分配的、
                 *   路由器里未必认得出哪个是它，只能从这儿看。
                 *   复制的是 IP:端口，手机 App 的输入框直接能粘。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "phoneUrlRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "手机地址"
                    descriptionText: "手机 App 的设置里填这个（IP:端口），不用配对"
                    stateText: Bridge.phoneAddr.length > 0
                        ? Bridge.phoneAddr : "还没有地址（板子没连上 WiFi）"
                    stateColor: Bridge.phoneAddr.length > 0
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "phoneUrlCopyButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "复制"
                        enabled: Bridge.phoneAddr.length > 0
                        onClicked: Bridge.copyToClipboard(Bridge.phoneAddr)
                    }
                }

                /*
                 * ★ 手机连接状态 ★
                 *   手机走 WiFi，板子按「最近有没有发过心跳」判断它在不在线。
                 *   这一行是用户唯一能确认「手机到底连上了没」的地方 ——
                 *   以前手机混在蓝牙列表里永远显示「已连接」，骗人。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "phoneStateRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "手机连接"
                    descriptionText: "手机 App 打开着就是已连接（走 WiFi，不走蓝牙）"
                    stateText: root.phoneStateText()
                    stateColor: root.phoneStateColor()

                    CompactButton {
                        objectName: "phoneStateRefreshButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        enabled: Bridge.connected
                        onClicked: Bridge.refreshRemotes()
                    }
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "phoneMappingRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "手机按键映射"
                    descriptionText: "在「按键」页选「手机（临时遥控器）」来改"
                    stateText: Bridge.phoneBinds + " 条"
                    stateColor: Bridge.phoneBinds > 0
                        ? tokens.successColor : tokens.disabledText
                }

                /*
                 * ★ 扫描板子看得见的 WiFi ★
                 *   板子只支持 2.4 GHz，而且 SSID 大小写必须完全一致 ——
                 *   手机上能连的那个名字，板子未必看得见（实测用户把
                 *   "CMCC-7K5j" 打成 "CMCC-7k5j"，板子一直找不到）。
                 *   与其让用户猜，不如让板子自己扫一遍，用户点名字。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "wifiScanRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "扫描周围 WiFi"
                    descriptionText: Bridge.wifiAps.length > 0
                        ? "扫到 " + Bridge.wifiAps.length + " 个，点名字填进上面的框"
                        : "列出开发板自己看得见的 WiFi（只有 2.4GHz）"
                    stateText: Bridge.wifiPicked.length > 0
                        ? Bridge.wifiPicked : ""
                    stateColor: tokens.successColor

                    CompactButton {
                        objectName: "wifiScanButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "扫描"
                        enabled: Bridge.connected
                        onClicked: Bridge.scanWifi()
                    }
                }

                Repeater {
                    model: Bridge.wifiAps
                    delegate: InlineSettingsRow {
                        id: ap
                        required property var modelData

                        tokens: root.tokens
                        objectName: "wifiApRow"
                        stateColumnWidth: root.stateColumnWidth
                        actionColumnWidth: root.actionColumnWidth
                        titleWidth: 180
                        titleText: ap.modelData.ssid
                        descriptionText: "信号 " + ap.modelData.rssi
                                         + " dBm · 信道 " + ap.modelData.chan
                        descriptionNeverElide: true
                        stateText: Bridge.wifiPicked === ap.modelData.ssid
                            ? "已选中" : ""
                        stateColor: tokens.successColor

                        CompactButton {
                            objectName: "wifiApPickButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth4Chars
                            text: "选中"
                            enabled: Bridge.connected
                            onClicked: Bridge.useWifiAp(ap.modelData.ssid)
                        }
                    }
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "wifiRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "开发板 WiFi"
                    descriptionText: "填路由器的 WiFi，开发板自己连上去"
                    editorColumnWidth: 260

                    editorData: [
                        CompactTextField {
                            id: wifiSsidField
                            objectName: "wifiSsidField"
                            tokens: root.tokens
                            Layout.preferredWidth: 120
                            placeholderText: "WiFi 名称"
                            Accessible.name: "WiFi 名称"
                        },
                        CompactTextField {
                            id: wifiPassField
                            objectName: "wifiPassField"
                            tokens: root.tokens
                            Layout.preferredWidth: 120
                            placeholderText: "WiFi 密码"
                            echoMode: TextInput.Password
                            Accessible.name: "WiFi 密码"
                        }
                    ]

                    CompactButton {
                        objectName: "wifiConnectButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "连接"
                        enabled: Bridge.connected
                        onClicked: Bridge.setWifi(wifiSsidField.text,
                                                 wifiPassField.text)
                    }
                }

                /*
                 * 记住的 WiFi —— 换地方不用重新配网。
                 * 板子最多记 6 个，开机按顺序试，哪个在场连哪个。
                 */
                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "wifiNetsRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "记住的 WiFi"
                    descriptionText: "最多记 6 个，换地方自动连在场的那个"
                    stateText: Bridge.wifiNets.length > 0
                        ? (Bridge.wifiNets.length + " 个") : "还没有"
                    stateColor: Bridge.wifiNets.length > 0
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "wifiNetsRefreshButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        enabled: Bridge.connected
                        onClicked: Bridge.refreshWifiNets()
                    }
                }

                Repeater {
                    model: Bridge.wifiNets
                    delegate: InlineSettingsRow {
                        id: netRow
                        required property var modelData
                        required property int index

                        tokens: root.tokens
                        objectName: "wifiNetRow"
                        stateColumnWidth: root.stateColumnWidth
                        actionColumnWidth: root.actionColumnWidth
                        titleWidth: 180
                        titleText: netRow.modelData.ssid
                        descriptionText: "第 " + (netRow.index + 1) + " 个尝试"
                        stateText: netRow.modelData.cur ? "当前在用" : "备用"
                        stateColor: netRow.modelData.cur
                            ? tokens.successColor : tokens.disabledText

                        CompactButton {
                            objectName: "wifiNetDeleteButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            compactMinimumWidth: root.tokens.buttonWidth4Chars
                            text: "删除"
                            enabled: Bridge.connected
                            onClicked: Bridge.deleteWifiNet(netRow.modelData.idx)
                        }
                    }
                }

                InlineSettingsRow {
                    tokens: root.tokens
                    objectName: "wifiStateRow"
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "联网状态"
                    descriptionText: Bridge.wifiWhy.length > 0
                        ? Bridge.wifiWhy
                        : "板子连上路由器后手机才能访问"
                    stateText: root.wifiStateText()
                    stateColor: root.wifiStateColor()

                    CompactButton {
                        objectName: "wifiRefreshButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        enabled: Bridge.connected
                        onClicked: Bridge.refreshWifi()
                    }
                }
            }

            Item { Layout.preferredHeight: root.tokens.pageVerticalPadding }
        }
    }
}
