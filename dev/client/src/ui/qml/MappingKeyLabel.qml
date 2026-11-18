/*
 * MappingKeyLabel.qml - 映射卡左侧的键名
 *
 * 照抄参考项目 windows-remote-mic-app 的 MappingKeyLabel.qml。
 */
import QtQuick

UiLabel {
    id: root

    font.pixelSize: root.tokens.fontSizeMappingKey
    font.weight: Font.Medium
    elide: Text.ElideRight
}
