// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "transcript-workflow",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "TranscriptWorkflow", targets: ["TranscriptWorkflow"]),
    ],
    targets: [
        .target(
            name: "TranscriptWorkflow",
            path: "Sources/TranscriptWorkflow"
        ),
        .testTarget(name: "TranscriptWorkflowTests", dependencies: ["TranscriptWorkflow"], path: "Tests")
    ]
)
