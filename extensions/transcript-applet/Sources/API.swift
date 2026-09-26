import Foundation

struct WordToken: Codable, Identifiable, Equatable {
    let i: Int
    let w: String
    let start: Double
    let end: Double
    var id: Int { i }
}

struct TimelineSlice: Codable, Identifiable, Equatable {
    let id: String
    let text: String
    let start: Double
    let end: Double
    let words: [WordToken]
}

struct TimelineSnapshot: Codable {
    let revision: String
    let title: String
    let duration: Double
    let slices: [TimelineSlice]
    let edit_error: String?
    var timeline: String? = nil
}

struct EditResponse: Codable {
    let ok: Bool
    let error: String?
    let story: TimelineSnapshot?
    var code: String? = nil
}

struct EditRequest: Encodable {
    let revision: String
    let action: String
    let id: String
    var before_id: String?
    var words: [Int] = []
    var source_words: [Int]?
    var anchor_words: [Int]?
    var timeline: String?
}

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

protocol TimelineAPI {
    func story() async throws -> TimelineSnapshot
    func edit(_ body: EditRequest) async throws -> EditResponse
}

struct APIClient: TimelineAPI, Sendable {
    let base: URL
    init(port: Int) { base = URL(string: "http://127.0.0.1:\(port)")! }

    private func request<T: Decodable>(_ path: String, body: Data? = nil) async throws -> T {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.timeoutInterval = 120
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let problem = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError(message: problem?["error"] as? String ?? "The timeline service could not complete the request.")
        }
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw APIError(message: problem?["error"] as? String ?? "The timeline service returned an incompatible response. Restart it with make ui.") }
    }
    func story() async throws -> TimelineSnapshot { try await request("api/editor") }
    func edit(_ body: EditRequest) async throws -> EditResponse {
        try await request("api/editor/edit", body: JSONEncoder().encode(body))
    }
}

func timecode(_ value: Double) -> String {
    let seconds = max(0, Int(value))
    return String(format: "%02d:%02d", seconds / 60, seconds % 60)
}
