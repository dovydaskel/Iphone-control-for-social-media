// BLE HID mouse controlled over USB serial.
//
// Serial protocol (115200 baud, one command per line, each answered with "OK ..." or "ERR ..."):
//   M dx dy      relative move; split into steps of at most STEP units, DELAY ms apart
//   R dx dy      send a single report right away (-127..127); the host controls timing
//   D / U        press / release left button (moves while pressed = drag)
//   C            click (down, hold, up)
//   W n          scroll wheel
//   STEP n       max units per HID report (1..127), default 10
//   DELAY ms     pause between move reports, default 8
//   ?            status
// Unsolicited events: "EVT connected", "EVT secured", "EVT disconnected".

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>

// Override per board in platformio.ini, e.g. -DDEVICE_NAME='"ESP Mouse 2"'
#ifndef DEVICE_NAME
#define DEVICE_NAME "ESP Mouse 1"
#endif

static const uint8_t MOUSE_REPORT_ID = 1;

static const uint8_t reportMap[] = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x02,        // Usage (Mouse)
    0xA1, 0x01,        // Collection (Application)
    0x85, MOUSE_REPORT_ID, //   Report ID
    0x09, 0x01,        //   Usage (Pointer)
    0xA1, 0x00,        //   Collection (Physical)
    0x05, 0x09,        //     Usage Page (Buttons)
    0x19, 0x01,        //     Usage Minimum (1)
    0x29, 0x03,        //     Usage Maximum (3)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x95, 0x03,        //     Report Count (3)
    0x75, 0x01,        //     Report Size (1)
    0x81, 0x02,        //     Input (Data, Variable, Absolute)
    0x95, 0x01,        //     Report Count (1)
    0x75, 0x05,        //     Report Size (5)
    0x81, 0x03,        //     Input (Constant) - padding
    0x05, 0x01,        //     Usage Page (Generic Desktop)
    0x09, 0x30,        //     Usage (X)
    0x09, 0x31,        //     Usage (Y)
    0x09, 0x38,        //     Usage (Wheel)
    0x15, 0x81,        //     Logical Minimum (-127)
    0x25, 0x7F,        //     Logical Maximum (127)
    0x75, 0x08,        //     Report Size (8)
    0x95, 0x03,        //     Report Count (3)
    0x81, 0x06,        //     Input (Data, Variable, Relative)
    0xC0,              //   End Collection
    0xC0               // End Collection
};

static NimBLEHIDDevice* hid;
static NimBLECharacteristic* mouseInput;
static volatile bool connected = false;
static volatile bool secured = false;
static uint8_t buttons = 0;
static int stepSize = 10;
static int stepDelayMs = 8;

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* server, NimBLEConnInfo& info) override {
        connected = true;
        Serial.println("EVT connected");
    }
    void onDisconnect(NimBLEServer* server, NimBLEConnInfo& info, int reason) override {
        connected = false;
        secured = false;
        buttons = 0;
        Serial.printf("EVT disconnected reason=%d\n", reason);
        NimBLEDevice::startAdvertising();
    }
    void onAuthenticationComplete(NimBLEConnInfo& info) override {
        secured = info.isEncrypted();
        Serial.printf("EVT secured=%d\n", secured ? 1 : 0);
    }
};

static void sendReport(int8_t dx, int8_t dy, int8_t wheel) {
    if (!connected) return;
    uint8_t report[4] = {buttons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
    mouseInput->setValue(report, sizeof(report));
    mouseInput->notify();
}

static void move(long dx, long dy) {
    while (dx != 0 || dy != 0) {
        int sx = constrain(dx, -stepSize, stepSize);
        int sy = constrain(dy, -stepSize, stepSize);
        sendReport(sx, sy, 0);
        dx -= sx;
        dy -= sy;
        delay(stepDelayMs);
    }
}

static void handleLine(String line) {
    line.trim();
    if (line.length() == 0) return;

    char cmd[8] = {0};
    long a = 0, b = 0;
    int n = sscanf(line.c_str(), "%7s %ld %ld", cmd, &a, &b);
    String c(cmd);
    c.toUpperCase();

    if (c == "?") {
        Serial.printf("OK name=%s connected=%d secured=%d step=%d delay=%d\n", DEVICE_NAME, connected, secured, stepSize, stepDelayMs);
        return;
    }
    if (c == "STEP" && n >= 2) {
        stepSize = constrain(a, 1, 127);
        Serial.printf("OK step=%d\n", stepSize);
        return;
    }
    if (c == "DELAY" && n >= 2) {
        stepDelayMs = constrain(a, 0, 1000);
        Serial.printf("OK delay=%d\n", stepDelayMs);
        return;
    }
    if (!connected) {
        Serial.println("ERR not connected");
        return;
    }
    if (c == "M" && n >= 3) {
        move(a, b);
    } else if (c == "R" && n >= 3) {
        sendReport(constrain(a, -127, 127), constrain(b, -127, 127), 0);
    } else if (c == "D") {
        buttons = 1;
        sendReport(0, 0, 0);
    } else if (c == "U") {
        buttons = 0;
        sendReport(0, 0, 0);
    } else if (c == "C") {
        buttons = 1;
        sendReport(0, 0, 0);
        delay(60);
        buttons = 0;
        sendReport(0, 0, 0);
    } else if (c == "W" && n >= 2) {
        sendReport(0, 0, constrain(a, -127, 127));
    } else {
        Serial.println("ERR unknown command");
        return;
    }
    Serial.println("OK");
}

void setup() {
    Serial.begin(115200);

    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setSecurityAuth(true, false, true);  // bonding, no MITM, secure connections
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    NimBLEServer* server = NimBLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());

    hid = new NimBLEHIDDevice(server);
    mouseInput = hid->getInputReport(MOUSE_REPORT_ID);
    hid->setManufacturer("DIY");
    hid->setPnp(0x02, 0xe502, 0xa111, 0x0210);
    hid->setHidInfo(0x00, 0x02);
    hid->setReportMap((uint8_t*)reportMap, sizeof(reportMap));
    hid->setBatteryLevel(100);

    server->start();

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->setName(DEVICE_NAME);
    adv->setAppearance(0x03C2);  // HID mouse
    adv->addServiceUUID(hid->getHidService()->getUUID());
    adv->enableScanResponse(true);
    adv->start();

    Serial.println("EVT ready");
}

void loop() {
    static String buf;
    while (Serial.available()) {
        char ch = Serial.read();
        if (ch == '\n') {
            handleLine(buf);
            buf = "";
        } else if (ch != '\r') {
            buf += ch;
        }
    }
    delay(1);
}
