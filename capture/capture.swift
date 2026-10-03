// Captures an iPhone screen over USB (the same source QuickTime uses) and writes
// JPEG frames to stdout, each prefixed with a 4-byte big-endian length.
//
//   capture --list              print "<uniqueID>\t<name>" for each connected iPhone
//   capture [--device <id>] [--fps 30] [--quality 0.6]
//
// Logs go to stderr.

import AVFoundation
import CoreImage
import CoreMediaIO
import Foundation

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func allowScreenCaptureDevices() {
    var prop = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var allow: UInt32 = 1
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil,
                              UInt32(MemoryLayout<UInt32>.size), &allow)
}

func iPhoneDevices() -> [AVCaptureDevice] {
    AVCaptureDevice.DiscoverySession(deviceTypes: [.external], mediaType: .muxed, position: .unspecified).devices
}

// Screen capture devices show up a moment after being allowed.
func waitForDevices(timeout: TimeInterval = 8) -> [AVCaptureDevice] {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let devices = iPhoneDevices()
        if !devices.isEmpty { return devices }
        RunLoop.current.run(until: Date().addingTimeInterval(0.25))
    }
    return []
}

final class FrameWriter: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let ciContext = CIContext()
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let minInterval: CFTimeInterval
    let quality: CGFloat
    var lastFrame: CFTimeInterval = 0
    let stdout = FileHandle.standardOutput

    init(fps: Double, quality: CGFloat) {
        minInterval = 1.0 / fps
        self.quality = quality
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        let now = CACurrentMediaTime()
        guard now - lastFrame >= minInterval,
              let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastFrame = now

        let image = CIImage(cvPixelBuffer: pixelBuffer)
        let options = [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): quality]
        guard let jpeg = ciContext.jpegRepresentation(of: image, colorSpace: colorSpace, options: options) else { return }

        var length = UInt32(jpeg.count).bigEndian
        let header = Data(bytes: &length, count: 4)
        do {
            try stdout.write(contentsOf: header + jpeg)
        } catch {
            exit(0)  // reader went away
        }
    }
}

let args = CommandLine.arguments
func argValue(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

signal(SIGPIPE, SIG_IGN)
allowScreenCaptureDevices()
let devices = waitForDevices()

if args.contains("--list") {
    for d in devices { print("\(d.uniqueID)\t\(d.localizedName)") }
    exit(0)
}

let wantedID = argValue("--device")
guard let device = devices.first(where: { wantedID == nil || $0.uniqueID == wantedID }) else {
    log("No iPhone found. Is it connected, unlocked and trusted?")
    exit(1)
}
log("Capturing \(device.localizedName) (\(device.uniqueID))")

let session = AVCaptureSession()
do {
    session.addInput(try AVCaptureDeviceInput(device: device))
} catch {
    log("Cannot open device: \(error.localizedDescription)")
    exit(1)
}

let output = AVCaptureVideoDataOutput()
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
output.alwaysDiscardsLateVideoFrames = true
let writer = FrameWriter(fps: Double(argValue("--fps") ?? "30") ?? 30,
                         quality: CGFloat(Double(argValue("--quality") ?? "0.6") ?? 0.6))
output.setSampleBufferDelegate(writer, queue: DispatchQueue(label: "frames"))
session.addOutput(output)

NotificationCenter.default.addObserver(forName: AVCaptureDevice.wasDisconnectedNotification, object: device, queue: nil) { _ in
    log("iPhone disconnected")
    exit(2)
}

session.startRunning()
RunLoop.main.run()
