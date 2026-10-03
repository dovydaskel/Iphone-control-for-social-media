// Recognises text in an image read from stdin (JPEG/PNG) using the Vision framework.
// Prints one JSON array: [{"text", "confidence", "x", "y", "w", "h"}] in image pixels,
// origin top-left.
//
//   ocr [--fast] < frame.jpg

import AppKit
import Foundation
import Vision

let data = FileHandle.standardInput.readDataToEndOfFile()
guard let image = NSImage(data: data),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot decode image\n".data(using: .utf8)!)
    exit(1)
}
let width = Double(cgImage.width), height = Double(cgImage.height)

let request = VNRecognizeTextRequest()
request.recognitionLevel = CommandLine.arguments.contains("--fast") ? .fast : .accurate
request.usesLanguageCorrection = false

do {
    try VNImageRequestHandler(cgImage: cgImage).perform([request])
} catch {
    FileHandle.standardError.write("OCR failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}

var results: [[String: Any]] = []
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox  // normalised, origin bottom-left
    results.append([
        "text": candidate.string,
        "confidence": candidate.confidence,
        "x": box.minX * width,
        "y": (1 - box.maxY) * height,
        "w": box.width * width,
        "h": box.height * height,
    ])
}
let json = try JSONSerialization.data(withJSONObject: results)
FileHandle.standardOutput.write(json)
