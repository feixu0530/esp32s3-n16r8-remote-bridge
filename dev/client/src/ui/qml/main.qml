/*
 * main.qml - 应用外壳
 * ---------------------------------------------------------------------------
 * 三页导航（设备 / 按键 / 语音），照抄 windows-remote-mic-app 的结构。
 * 关闭窗口时按设置隐藏到任务栏（托盘）。
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: 1180
    height: 840
    minimumWidth: 940
    minimumHeight: 620
    visible: true
    color: th.background
    title: Bridge.appTitle

    Tokens { id: th }

    property int currentPage: 0
    readonly property var pages: [
        { title: "设备", glyph: "\uE772" },
        { title: "按键", glyph: "\uE765" },
        { title: "语音", glyph: "\uE720" }
    ]

    /*
     * 关闭窗口时隐藏到任务栏。
     * closeBehavior = "hide" 时吃掉这次关闭，改成隐藏窗口；
     * = "quit" 时正常退出。
     * ★ 托盘不可用时不能隐藏 ★ —— 否则窗口藏起来就再也找不回来了。
     */
    onClosing: function(close) {
        if (Bridge.closeBehavior === "hide" && App.trayAvailable) {
            close.accepted = false
            win.hide()
        }
    }

    /*
     * 启动时自动连开发板。
     * connectDefaultPort() 的返回值：
     *   ""        没有开发板
     *   "<端口>"  已经连上了
     *   "__ask__" 插了多个 —— 不猜，问用户
     */
    Component.onCompleted: {
        var r = Bridge.connectDefaultPort()
        if (r === "__ask__")
            portChoiceDialog.open()
    }

    /* 插了多个开发板 —— 让用户选，不猜 */
    SettingsDialog {
        id: portChoiceDialog
        tokens: th
        objectName: "portChoiceDialog"
        title: "选择开发板"
        preferredWidth: 380
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            if (choiceBox.currentIndex >= 0)
                Bridge.connectPort(choiceBox.currentText)
        }

        contentItem: ColumnLayout {
            spacing: th.spacingSmall
            width: 340

            Text {
                text: "检测到多个开发板，请选择要连的那个。"
                color: th.textPrimary
                font.family: th.fontFamily
                font.pixelSize: th.fontSizeBody
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            SelectionComboBox {
                id: choiceBox
                objectName: "choiceBox"
                tokens: th
                Layout.fillWidth: true
                model: Bridge.portCandidates
                currentIndex: 0
            }
        }
    }


    RowLayout {
        anchors.fill: parent
        spacing: 0

        // ------------------------------------------------------ 左侧导航
        Rectangle {
            Layout.preferredWidth: 84
            Layout.fillHeight: true
            color: th.sidebar

            Rectangle {
                anchors.right: parent.right
                width: 1
                height: parent.height
                color: th.border
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 4

                Repeater {
                    model: win.pages
                    delegate: NavButton {
                        required property int index
                        tokens: th
                        glyph: win.pages[index].glyph
                        text: win.pages[index].title
                        checkable: true
                        checked: win.currentPage === index
                        onClicked: win.currentPage = index
                        Layout.fillWidth: true
                    }
                }

                Item { Layout.fillHeight: true }

            }
        }

        // ------------------------------------------------------ 右侧内容
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: win.currentPage

                DevicePage { tokens: th }
                ButtonsPage { tokens: th }
                VoicePage { tokens: th }
            }

            // ------------------------------------------------ 底部状态栏
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 26
                color: th.surface

                Rectangle {
                    width: parent.width
                    height: 1
                    color: th.border
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    spacing: 14

                    Text {
                        text: Bridge.statusMessage
                        color: th.textSecondary
                        font.family: th.fontFamily
                        font.pixelSize: th.fontSizeSmall
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }

                    Text {
                        text: Bridge.firmwareVersion.length > 0
                              ? "固件 " + Bridge.firmwareVersion : "固件 —"
                        color: th.disabledText
                        font.family: th.fontFamily
                        font.pixelSize: th.fontSizeSmall
                    }

                    Text {
                        text: "v" + Bridge.appVersion
                        color: th.disabledText
                        font.family: th.fontFamily
                        font.pixelSize: th.fontSizeSmall
                    }
                }
            }
        }
    }
}
