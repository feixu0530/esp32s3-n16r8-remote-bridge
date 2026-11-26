/*
 * MappingCard.qml - 一个物理键的映射卡
 * ---------------------------------------------------------------------------
 * ★ 照抄参考项目 ★
 *   reference/winmic-src/apps/windows/rc003/src/ovb_rc003/qml/MappingCard.qml
 *   （261 行，逐段搬过来）
 *
 * 结构：
 *   左边是键名（MappingKeyLabel），右边是三格 —— 单击 / 双击 / 长按。
 *   语音键（mic）特殊：只显示语音页录制的热键和修改提示，不参与按键映射。
 *
 * 整张卡是一个按钮：点一下打开动作编辑器。
 */
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AbstractButton {
    id: root

    property var tokens
    property string cardId: ""
    property string buttonName: ""
    property real keyLabelWidth: 32
    property string singleText: ""
    property string doubleText: ""
    property string longText: ""
    property string singleNoteText: ""
    property string doubleNoteText: ""
    property string longNoteText: ""
    property bool selected: false
    property bool voiceAction: false
    property bool exposeObjectNames: true

    /* 自定义键的就地管理：点名字改名、右键删除 */
    signal renameRequested(string cardId)
    signal deleteRequested(string cardId)

    hoverEnabled: true
    implicitHeight: 49
    leftPadding: 4
    rightPadding: 4
    topPadding: 3
    bottomPadding: 3
    objectName: exposeObjectNames ? "editMapping_" + cardId : ""
    Accessible.name: buttonName + qsTr("按键映射")

    TapHandler {
        acceptedButtons: Qt.RightButton
        onTapped: root.deleteRequested(root.cardId)
    }

    /* 动作名的简写，跟参考项目一致 */
    function displayBinding(text) {
        const shortcuts = {
            "Escape": "Esc",
            "Return": "Enter",
            "回车": "Enter",
            "Delete": "Del",
            "Delete（退格）": "Del",
            "退格": "Del",
            "方向上": "↑",
            "方向下": "↓",
            "方向左": "←",
            "方向右": "→",
            "系统音量 +": "Vol +",
            "系统音量 −": "Vol -"
        }
        if (shortcuts[text])
            return shortcuts[text]
        return text ? text.replace(/\+/g, " + ") : ""
    }

    component GestureCell: Item {
        id: cell
        property string gestureLabel: ""
        property string bindingText: ""
        property string noteText: ""
        property string valueObjectName: ""
        readonly property bool empty:
            !bindingText || bindingText.trim().length === 0
            || bindingText.trim() === qsTr("未设置")
        readonly property bool usingNote:
            !empty && noteText && noteText.trim().length > 0
            && noteText.trim() !== qsTr("未命名")
        readonly property string shownText: empty
            ? qsTr("未设置")
            : usingNote
                ? noteText.trim() : root.displayBinding(bindingText.trim())

        Layout.fillWidth: true
        Layout.fillHeight: true
        implicitHeight: 30

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: root.tokens.hairlineWidth
            color: root.tokens.border
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 3
            anchors.rightMargin: 3
            spacing: 0

            UiLabel {
                id: gestureTitle
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.preferredHeight: 13
                text: cell.gestureLabel
                color: root.tokens.textSecondary
                font.pixelSize: root.tokens.fontSizeMapGesture
                font.weight: Font.Normal
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                HoverHandler { id: gestureHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: gestureHover.hovered && !cell.empty
                        && (cell.usingNote || valueLabel.truncated)
                    text: cell.usingNote
                        ? cell.noteText.trim() + " · " + cell.bindingText.trim()
                        : cell.bindingText.trim()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 22
                radius: 3
                color: !cell.usingNote && !cell.empty
                    ? root.tokens.surfaceMuted : "transparent"

                UiLabel {
                    id: valueLabel
                    objectName: cell.valueObjectName
                    anchors.fill: parent
                    anchors.leftMargin: 3
                    anchors.rightMargin: 3
                    tokens: root.tokens
                    kind: bodyKind
                    text: cell.shownText
                    color: cell.empty
                        ? root.tokens.disabledText
                        : root.tokens.textPrimary
                    font.family: cell.usingNote
                        ? root.tokens.fontFamily : root.tokens.fontFamilyMono
                    font.pixelSize: cell.usingNote
                        ? root.tokens.fontSizeSmall
                        : root.tokens.fontSizeMapGesture
                    font.weight: Font.Normal
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
            }
        }
    }

    /* 语音键：只显示语音页录制的热键，并指向语音页修改 */
    component VoiceRecordedCell: Item {
        id: voiceCell
        readonly property bool hasKey:
            !!root.singleText && root.singleText.trim().length > 0
            && root.singleText.trim() !== qsTr("未设置")
            && root.singleText.trim() !== qsTr("（未设置）")

        Layout.fillWidth: true
        Layout.fillHeight: true
        implicitHeight: 34

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            UiLabel {
                objectName: root.exposeObjectNames
                    ? "mappingVoiceKeyText_" + root.cardId : ""
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                Layout.preferredHeight: 22
                text: voiceCell.hasKey
                    ? root.displayBinding(root.singleText.trim()) : qsTr("未设置")
                color: voiceCell.hasKey
                    ? root.tokens.textPrimary : root.tokens.disabledText
                font.family: root.tokens.fontFamilyMono
                font.pixelSize: root.tokens.fontSizeMapGesture
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            UiLabel {
                objectName: root.exposeObjectNames
                    ? "mappingVoiceHint_" + root.cardId : ""
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.preferredHeight: 12
                text: qsTr("修改语音键映射到语音页录制")
                color: root.tokens.disabledText
                font.pixelSize: root.tokens.fontSizeSmall - 1
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusSmall
        color: root.selected ? root.tokens.accentSoft : root.tokens.surface
        border.width: root.tokens.hairlineWidth
        border.color: root.selected
            ? root.tokens.accent
            : root.hovered
                ? root.tokens.borderStrong : root.tokens.cardBorder
    }

    contentItem: GridLayout {
        columns: 2
        columnSpacing: 4

        MappingKeyLabel {
            id: keyLabel
            objectName: root.exposeObjectNames
                ? "mappingKeyCell_" + root.cardId : ""
            tokens: root.tokens
            Layout.preferredWidth: root.keyLabelWidth
            Layout.minimumWidth: root.keyLabelWidth
            Layout.maximumWidth: root.keyLabelWidth
            Layout.fillHeight: true
            text: root.buttonName
            Accessible.name: text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter

            MouseArea {
                objectName: root.exposeObjectNames
                    ? "mappingKeyName_" + root.cardId : ""
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onClicked: root.renameRequested(root.cardId)
            }
        }

        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: root.voiceAction ? 1 : 3
            columnSpacing: 0
            rowSpacing: 0

            GestureCell {
                objectName: root.exposeObjectNames
                    ? "mappingSingleCell_" + root.cardId : ""
                visible: !root.voiceAction
                gestureLabel: qsTr("单击")
                bindingText: root.singleText
                noteText: root.singleNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingSinglePrimaryText_" + root.cardId : ""
            }

            GestureCell {
                objectName: root.exposeObjectNames
                    ? "mappingDoubleCell_" + root.cardId : ""
                visible: !root.voiceAction
                gestureLabel: qsTr("双击")
                bindingText: root.doubleText
                noteText: root.doubleNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingDoublePrimaryText_" + root.cardId : ""
            }

            GestureCell {
                objectName: root.exposeObjectNames
                    ? "mappingLongCell_" + root.cardId : ""
                visible: !root.voiceAction
                gestureLabel: qsTr("长按")
                bindingText: root.longText
                noteText: root.longNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingLongPrimaryText_" + root.cardId : ""
            }

            VoiceRecordedCell {
                objectName: root.exposeObjectNames
                    ? "mappingVoiceRecordedCell_" + root.cardId : ""
                visible: root.voiceAction
            }
        }
    }
}
