/*
 * VoicePage.qml - 语音页
 * ---------------------------------------------------------------------------
 * 照抄 windows-remote-mic-app 的 VoicePage.qml 结构：
 *   音频前置 / 语音程序
 *
 * 与参考项目的差别：
 *   - 不需要虚拟声卡 —— **开发板自己就是 USB 麦克风**，
 *     Windows 直接把它当成录音设备，语音程序不用再经过任何中间层
 *   - 「语音按键」存在开发板的 NVS 里（VOICEKEY），
 *     拔掉 PC，板子按住遥控器语音键照样能触发输入法
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var tokens: Tokens {}

    readonly property int stateColumnWidth: 110
    readonly property int actionColumnWidth: tokens.buttonWidth6Chars

    readonly property var imeProviders: ["none", "sogou", "wechat", "doubao", "custom"]
    readonly property var imeLabels: ["不使用", "搜狗输入法", "微信输入法",
                                     "豆包输入法", "自定义"]

    onVisibleChanged: {
        if (visible) {
            Bridge.refreshVoiceKey()
        }
    }

    ScrollView {
        id: scroll
        objectName: "voiceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: content
            objectName: "voicePageContent"
            width: Math.max(0, scroll.availableWidth - root.tokens.pageHorizontalPadding * 2)
            x: root.tokens.pageHorizontalPadding
            y: root.tokens.pageVerticalPadding
            spacing: root.tokens.spacingMedium

            // ============================================== 音频前置
            SectionFrame {
                objectName: "audioSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "audioSectionTitle"
                    text: "音频前置"
                }

                InlineSettingsRow {
                    objectName: "boardMicRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "开发板麦克风"
                    descriptionText: "开发板通过 USB 直接充当系统麦克风，"
                                   + "不需要安装虚拟声卡"
                    descriptionNeverElide: true
                    stateText: Bridge.connected ? "已就绪" : "未连接"
                    stateColor: Bridge.connected
                        ? tokens.successColor : tokens.disabledText

                    CompactButton {
                        objectName: "refreshVoiceKeyButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth4Chars
                        text: "刷新"
                        onClicked: Bridge.refreshVoiceKey()
                    }
                }

                InlineSettingsRow {
                    objectName: "voiceChannelRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "语音通道"
                    descriptionText: "按住遥控器的语音键说话"
                    descriptionNeverElide: true
                    stateText: Bridge.voiceState.length > 0
                        ? Bridge.voiceState : "空闲"
                    stateColor: Bridge.voiceState.length > 0
                        ? tokens.voiceAccent : tokens.disabledText

                    CompactSwitch {
                        objectName: "voiceChannelSwitch"
                        tokens: root.tokens
                        checked: Bridge.voiceEnabled
                        Accessible.name: "语音通道"
                        onToggled: {
                            if (checked !== Bridge.voiceEnabled)
                                Bridge.setVoiceEnabled(checked)
                        }
                    }
                }
            }

            // ============================================== 语音程序
            SectionFrame {
                objectName: "voiceProgramSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    tokens: root.tokens
                    objectName: "voiceProgramSectionTitle"
                    text: "语音程序"
                }

                InlineSettingsRow {
                    objectName: "imeProviderRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "选择输入法"
                    descriptionText: "开发板把声音直接送给 Windows，"
                                   + "由选中的输入法接收并出字"
                    descriptionNeverElide: true

                    editorData: [SelectionComboBox {
                        id: providerCombo
                        objectName: "providerCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.minimumWidth: 180
                        model: root.imeLabels
                        currentIndex: Math.max(0, root.imeProviders.indexOf(Bridge.imeProvider))
                        onActivated: function (index) {
                            Bridge.setImeProvider(root.imeProviders[index])
                        }
                        Accessible.name: "选择输入法"
                    },
                    CompactButton {
                        objectName: "openImeSettingsButton"
                        tokens: root.tokens
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: "输入法设置"
                        onClicked: Bridge.openImeSettings()
                    }]
                }

                InlineSettingsRow {
                    objectName: "voiceModeRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "按键模式"
                    descriptionText: "按住说话，松开结束（开发板仅支持按住型）"
                    descriptionNeverElide: true

                    editorData: [SelectionComboBox {
                        id: modeCombo
                        objectName: "modeCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.minimumWidth: 180
                        model: ["按住型"]
                        currentIndex: 0
                        enabled: false
                        Accessible.name: "按键模式"
                    }]
                }

                InlineSettingsRow {
                    objectName: "voiceHotkeyRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "语音按键"
                    descriptionText: Bridge.voiceKeyText.length > 0
                        ? "开发板当前生效：" + Bridge.voiceKeyText
                          + (Bridge.imeHotkeyCustom
                             ? "" : "（未在软件里改过，不会覆盖板子设置）")
                        : "还没读到开发板上的设置"
                    descriptionNeverElide: true
                    stateText: Bridge.hotkeyCapturing ? "录制中" : ""
                    stateColor: tokens.voiceAccent

                    editorData: [Item {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 150
                        implicitHeight: root.tokens.controlHeight

                        CompactTextField {
                            id: hotkeyField
                            objectName: "voiceHotkeyField"
                            anchors.fill: parent
                            tokens: root.tokens
                            text: Bridge.imeHotkey
                            placeholderText: "例如 lctrl+lwin"
                            readOnly: Bridge.hotkeyCapturing
                            onEditingFinished: Bridge.setImeHotkey(text)
                            // ★ 边输边生效 ★
                            //   只在失焦时才保存的话，用户改完立刻按键 →
                            //   板子还是旧热键，输入法当然不响应（实测踩过）。
                            onTextEdited: hotkeyApplyTimer.restart()
                            Accessible.name: "语音按键"
                        }
                    }]

                    CompactButton {
                        objectName: "recordVoiceHotkeyButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: root.tokens.buttonWidth6Chars
                        text: Bridge.hotkeyCapturing ? "停止录制" : "录制快捷键"
                        highlighted: Bridge.hotkeyCapturing
                        onClicked: {
                            if (Bridge.hotkeyCapturing)
                                Bridge.stopHotkeyCapture()
                            else
                                Bridge.startHotkeyCapture()
                        }
                    }
                }

                /*
                 * ★ 最近一次语音触发 ★
                 *   按了没反应的时候，这一行能立刻分清：
                 *     · 这一行是空的 → 板子压根没收到语音键
                 *     · 这一行有时间、有遥控器 → 板子收到并按出了快捷键，
                 *       后面没出字就是输入法那边的事。
                 */
                InlineSettingsRow {
                    objectName: "lastVoiceEventRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "最近触发"
                    descriptionText: Bridge.lastVoiceEvent.length > 0
                        ? Bridge.lastVoiceEvent
                        : "还没收到过遥控器的语音键"
                    descriptionNeverElide: true
                    stateText: Bridge.lastVoiceEvent.length > 0 ? "收到过" : "无"
                    stateColor: Bridge.lastVoiceEvent.length > 0
                        ? tokens.successColor : tokens.disabledText
                }

                /* ★ 每台遥控器的在线状态（语音键要按在线的那台） ★ */
                InlineSettingsRow {
                    objectName: "remoteVoiceStatusRow"
                    tokens: root.tokens
                    stateColumnWidth: root.stateColumnWidth
                    actionColumnWidth: root.actionColumnWidth
                    titleText: "遥控器"
                    descriptionText: Bridge.remoteOnlineText.length > 0
                        ? Bridge.remoteOnlineText
                        : "还没有配对任何遥控器"
                    descriptionNeverElide: true
                    stateText: Bridge.remoteOnlineCount + " 台在线"
                    stateColor: Bridge.remoteOnlineCount > 0
                        ? tokens.successColor : tokens.disabledText
                }
            }

            UiLabel {
                objectName: "voiceHotkeyNote"
                tokens: root.tokens
                kind: 1
                Layout.fillWidth: true
                Layout.leftMargin: root.tokens.spacingLarge
                Layout.rightMargin: root.tokens.spacingLarge
                wrapMode: Text.WordWrap
                text: "语音快捷键的默认映射是**左Ctrl + 左Win**。"
                      + "只要没在这个页面改过，客户端连上开发板时"
                      + "只会读取板子当前生效的值，不会覆盖它。"
            }

            UiLabel {
                objectName: "voiceNote"
                tokens: root.tokens
                kind: 1
                Layout.fillWidth: true
                Layout.leftMargin: root.tokens.spacingLarge
                Layout.rightMargin: root.tokens.spacingLarge
                wrapMode: Text.WordWrap
                text: "按住型录音期间，语音快捷键会保持按下。"
                      + "设置快捷键时，请留意与常用操作的冲突。"
            }

            Item { Layout.preferredHeight: root.tokens.pageVerticalPadding }
        }
    }

    Connections {
        target: Bridge
        function onHotkeyCaptureChanged() {
            if (!Bridge.hotkeyCapturing && Bridge.capturedHotkey.length > 0) {
                hotkeyField.text = Bridge.capturedHotkey
                // ★ 录制完立即写入 ★
                //   这里只是给输入框赋文本，不会触发 textEdited /
                //   editingFinished —— 以前录完就「只显示不保存」，
                //   用户按下去还是旧热键。
                Bridge.setImeHotkey(Bridge.capturedHotkey)
            }
        }
    }

    Timer {
        id: hotkeyApplyTimer
        interval: 700
        repeat: false
        onTriggered: Bridge.setImeHotkey(hotkeyField.text)
    }
}
