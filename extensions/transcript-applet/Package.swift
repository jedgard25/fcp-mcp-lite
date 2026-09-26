// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "transcript-applet",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "TranscriptApplet",
            path: "Sources"
        ),
        .testTarget(name: "TranscriptAppletTests", dependencies: ["TranscriptApplet"], path: "Tests")
    ]
)
