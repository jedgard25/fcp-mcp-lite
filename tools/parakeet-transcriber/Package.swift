// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "parakeet-transcriber",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/FluidInference/FluidAudio.git", exact: "0.12.6"),
    ],
    targets: [
        .executableTarget(
            name: "parakeet-transcriber",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            path: "Sources"
        ),
    ]
)
