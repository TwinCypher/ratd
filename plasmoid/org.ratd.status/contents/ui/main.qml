/*
    Mouse status from ratd. Reads GetState once the service appears, then follows
    StateChanged, which carries the whole state as JSON.
*/
import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string busName: "org.ratd"
    readonly property string objPath: "/org/ratd"
    readonly property string iface: "org.ratd.Manager"
    readonly property bool serviceUp: serviceWatcher.registered

    property var ratd: ({})
    property var profiles: []
    readonly property var device: (ratd.devices && ratd.devices.length)
                                  ? (ratd.devices.find(d => d.connected) || ratd.devices[0]) : null
    readonly property bool lowBattery: device !== null && device.battery !== null
                                       && device.battery <= 15 && device.battery_status === "discharging"

    function call(member, args, signature, onValue) {
        DBus.SessionBus.asyncCall({
            service: busName, path: objPath, iface: iface, member: member,
            arguments: args || [], signature: signature || "",
        }, reply => { if (onValue) onValue(reply.value) }, () => {})
    }

    function refresh() {
        if (!serviceUp) {
            ratd = {};
            profiles = [];
            return;
        }
        call("GetState", [], "", v => ratd = JSON.parse(String(v)));
        call("GetConfig", [], "", v => {
            const cfg = JSON.parse(String(v));
            const dev = device ? cfg.devices[device.key] : Object.values(cfg.devices)[0];
            profiles = dev ? Object.keys(dev.profiles) : [];
        });
    }

    function compactText() {
        if (!serviceUp) return "off";
        if (!device) return "no mouse";
        if (!device.connected) return "asleep";
        let t = (device.dpi ? String(device.dpi) : "?") + " DPI";
        if (device.battery !== null) t += " · " + device.battery + "%";
        return t;
    }

    function profileReason() {
        if (ratd.override) return "forced";
        if (!ratd.focus || !ratd.focus.class) return "default";
        return "for " + ratd.focus.class;
    }

    DBus.DBusServiceWatcher {
        id: serviceWatcher
        busType: DBus.BusType.Session
        watchedService: root.busName
        onRegisteredChanged: root.refresh()
    }

    DBus.SignalWatcher {
        busType: DBus.BusType.Session
        service: root.busName
        path: root.objPath
        iface: root.iface
        enabled: root.serviceUp

        function dbusStateChanged(payload) {
            const before = root.device ? root.device.key : "";
            root.ratd = JSON.parse(String(payload));
            if (root.device && root.device.key !== before)
                root.refresh();
        }
    }

    Component.onCompleted: refresh()

    Plasmoid.icon: "input-mouse"
    Plasmoid.status: serviceUp ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus
    toolTipMainText: device ? device.name : "ratd"
    toolTipSubText: !serviceUp ? "Service not running"
                  : !device ? "No mouse found"
                  : !device.connected ? "Asleep"
                  : device.dpi + " DPI · profile " + device.profile + " (" + profileReason() + ")"
                    + (device.battery !== null ? "\nBattery " + device.battery + "%, " + device.battery_status : "")

    compactRepresentation: MouseArea {
        readonly property int pad: Kirigami.Units.largeSpacing
        Layout.minimumWidth: row.implicitWidth + 2 * pad
        Layout.preferredWidth: row.implicitWidth + 2 * pad
        hoverEnabled: true
        onClicked: root.expanded = !root.expanded

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.smallSpacing * 2

            Kirigami.Icon {
                source: "input-mouse"
                Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
                opacity: root.device && root.device.connected ? 1 : 0.5
            }
            ColumnLayout {
                spacing: 0
                PlasmaComponents.Label {
                    text: root.device && root.device.profile ? root.device.profile : "ratd"
                    font.bold: true
                    font.italic: !!root.ratd.override
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                }
                PlasmaComponents.Label {
                    text: root.compactText()
                    color: root.lowBattery ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                    font.features: { "tnum": 1 }
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                }
            }
        }
    }

    fullRepresentation: ColumnLayout {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 14
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 3
            text: root.device ? root.device.name : (root.serviceUp ? "No mouse found" : "ratd isn't running")
        }

        GridLayout {
            columns: 2
            visible: root.device !== null
            columnSpacing: Kirigami.Units.largeSpacing

            PlasmaComponents.Label { text: "DPI"; opacity: 0.7 }
            PlasmaComponents.Label { text: root.device && root.device.dpi ? root.device.dpi : "-" }
            PlasmaComponents.Label { text: "Polling"; opacity: 0.7 }
            PlasmaComponents.Label { text: root.device && root.device.report_rate ? root.device.report_rate + " Hz" : "-" }
            PlasmaComponents.Label { text: "Battery"; opacity: 0.7 }
            PlasmaComponents.Label {
                text: root.device && root.device.battery !== null
                      ? root.device.battery + "%, " + root.device.battery_status : "-"
                color: root.lowBattery ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
            }
            PlasmaComponents.Label { text: "Profile"; opacity: 0.7 }
            PlasmaComponents.Label {
                text: root.device && root.device.profile ? root.device.profile + " (" + root.profileReason() + ")" : "-"
            }
            PlasmaComponents.Label { text: "Status"; opacity: 0.7 }
            PlasmaComponents.Label { text: root.device && root.device.connected ? "Connected" : "Asleep" }
        }

        Kirigami.Separator { Layout.fillWidth: true; visible: root.profiles.length > 0 }

        PlasmaComponents.Label { text: "Profile"; opacity: 0.7; visible: root.profiles.length > 0 }
        Flow {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            visible: root.profiles.length > 0

            PlasmaComponents.Button {
                text: "Automatic"
                checkable: true
                checked: !root.ratd.override
                onClicked: root.call("SetProfile", [""], "s")
            }
            Repeater {
                model: root.profiles
                PlasmaComponents.Button {
                    required property string modelData
                    text: modelData
                    checkable: true
                    checked: root.ratd.override === modelData
                    onClicked: root.call("SetProfile", [modelData], "s")
                }
            }
        }

        Kirigami.Separator { Layout.fillWidth: true }

        PlasmaComponents.Button {
            Layout.alignment: Qt.AlignRight
            icon.name: "configure"
            text: "Settings…"
            enabled: root.serviceUp
            onClicked: {
                root.call("OpenSettings");
                root.expanded = false;
            }
        }
    }
}
