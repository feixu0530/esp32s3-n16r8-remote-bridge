/*
 * EditorActionCombo.qml - 可编辑、带分组的动作下拉
 *
 * 照抄参考项目 windows-remote-mic-app 的 ButtonsPage.qml:556-668
 * （里面的 inline component EditorActionCombo）。
 *
 * ★ 用户反馈的两个毛病，这里都改了 ★
 *   1. 「弹出的位置从中间开始」
 *      → delegate 以前没绑 ListView.view.width，列表宽度跟输入框对不上；
 *        现在绑上了，并且显式写 popup（紧贴控件下沿展开、宽度与控件一致）。
 *   2. 「分类的字体比选项小，显示的UI比较奇怪」
 *      → 分类标题以前写死 font.pixelSize: 8，比选项小一大截；
 *        现在跟参考项目一样用 fontSizeTiny + Medium 字重 + 次要色，
 *        并补上 height / verticalAlignment / elide（以前被挤歪）。
 */
import QtQuick
import QtQuick.Controls

ComboBox {
    id: editorCombo

    property var tokens

    /** 把一个值设进框里（预设就选中它，不是预设就当自由文本） */
    function setEditorValue(value) {
        const display = String(value === undefined || value === null ? "" : value)
        currentIndex = -1
        for (let i = 0; i < model.length; ++i) {
            if (String(model[i]) === display) {
                currentIndex = i
                break
            }
        }
        editText = display
    }

    editable: true
    selectTextByMouse: true
    implicitHeight: tokens ? tokens.controlHeight : 28
    leftPadding: 7
    rightPadding: 22
    font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
    font.pixelSize: tokens ? tokens.fontSizeControl : 11

    indicator: DropDownIndicator {
        x: editorCombo.width - width - 7
        y: (editorCombo.height - height) / 2
    }

    background: Rectangle {
        radius: tokens.cornerRadiusControl
        color: tokens.fieldBackground
        border.width: tokens.hairlineWidth
        border.color: editorCombo.activeFocus ? tokens.accent : tokens.border
    }

    /*
     * ★ 弹出位置钉死 ★
     *   紧贴控件下沿展开，宽度与控件一致 ——
     *   默认定位会受外层 Dialog 的布局影响，弹出位置看着像「从中间开始」。
     */
    popup: Popup {
        y: editorCombo.height + 2
        width: editorCombo.width
        padding: 1
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 320)
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: editorCombo.popup.visible ? editorCombo.delegateModel : null
            currentIndex: editorCombo.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
        }
        background: Rectangle {
            radius: tokens.cornerRadiusControl
            color: tokens.surface
            border.width: tokens.hairlineWidth
            border.color: tokens.border
        }
    }

    delegate: ItemDelegate {
        id: optionDelegate

        readonly property string groupTitle:
            Bridge.actionOptionGroupTitle(String(modelData))
        readonly property bool startsGroup:
            groupTitle.length > 0 && (index === 0
                || groupTitle !== Bridge.actionOptionGroupTitle(
                    String(editorCombo.model[index - 1])))
        readonly property int groupHeaderHeight: startsGroup
            ? Math.ceil(tokens.fontSizeTiny) + tokens.spacingMedium : 0

        objectName: editorCombo.objectName + "_option_" + index
        width: ListView.view ? ListView.view.width : editorCombo.width
        height: tokens.controlHeight + groupHeaderHeight
        topPadding: groupHeaderHeight
        bottomPadding: 0
        leftPadding: 7
        rightPadding: 7
        highlighted: editorCombo.highlightedIndex === index

        contentItem: Label {
            text: String(modelData)
            color: tokens.textPrimary
            font.family: tokens.fontFamily
            font.pixelSize: tokens.fontSizeControl
            font.weight: index === editorCombo.currentIndex
                ? Font.DemiBold : Font.Normal
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Item {
            Rectangle {
                visible: optionDelegate.startsGroup
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: tokens.hairlineWidth
                color: tokens.border
            }
            Label {
                visible: optionDelegate.startsGroup
                anchors.top: parent.top
                anchors.topMargin: 1
                anchors.left: parent.left
                anchors.leftMargin: 7
                anchors.right: parent.right
                anchors.rightMargin: 7
                height: optionDelegate.groupHeaderHeight - 1
                text: optionDelegate.groupTitle
                color: tokens.textSecondary
                font.family: tokens.fontFamily
                /* ★ 跟参考项目一致：分类标题比选项小一点，但有字重、有次要色 ★ */
                font.pixelSize: tokens.fontSizeTiny
                font.weight: Font.Medium
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: tokens.controlHeight
                color: optionDelegate.highlighted
                    ? tokens.accentSoft : tokens.surface
            }
        }

        /* 点分类标题不会误选这一组的第一个选项 */
        MouseArea {
            visible: optionDelegate.startsGroup
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: optionDelegate.groupHeaderHeight
            acceptedButtons: Qt.LeftButton
        }
        Accessible.name: String(modelData)
    }
}
