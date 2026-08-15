#!/usr/bin/env swift
// Things URL Scheme bridge. The authorization token is read directly from the
// macOS Keychain and never enters argv, stdout, receipt data, or Git.

import AppKit
import Foundation
import Security

struct Request: Decodable {
    let action: String
    let title: String?
    let when: String?
    let tags: [String]?
    let list: String?
    let existingID: String?
    let canceled: Bool?
    let notes: String?
    let callbackURL: String?
}

enum BridgeError: Error, LocalizedError {
    case invalidRequest(String)
    case keychain(OSStatus)
    case invalidURL
    case dispatchFailed

    var errorDescription: String? {
        switch self {
        case .invalidRequest(let message): return message
        case .keychain: return "Things authorization token is unavailable in Keychain"
        case .invalidURL: return "could not construct a Things URL"
        case .dispatchFailed: return "macOS could not dispatch the Things URL"
        }
    }
}

func writeJSON(_ value: [String: String]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func authorizationToken() throws -> String {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "woon.second-brain.things-url-scheme",
        kSecAttrAccount as String: "things-url-scheme",
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess, let data = result as? Data,
          let token = String(data: data, encoding: .utf8), !token.isEmpty else {
        throw BridgeError.keychain(status)
    }
    return token
}

func addQueryItem(_ name: String, _ value: String?, to items: inout [URLQueryItem]) {
    if let value, !value.isEmpty { items.append(URLQueryItem(name: name, value: value)) }
}

func buildURL(_ request: Request) throws -> URL {
    let command: String
    switch request.action {
    case "permission":
        _ = try authorizationToken()
        throw BridgeError.invalidRequest("permission is not a URL command")
    case "add": command = "add"
    case "update": command = "update"
    default: throw BridgeError.invalidRequest("unsupported Things action")
    }
    guard var components = URLComponents(string: "things:///\(command)") else {
        throw BridgeError.invalidURL
    }
    var items: [URLQueryItem] = []
    addQueryItem("title", request.title, to: &items)
    addQueryItem("when", request.when, to: &items)
    addQueryItem("list", request.list, to: &items)
    addQueryItem("notes", request.notes, to: &items)
    if let tags = request.tags, !tags.isEmpty {
        items.append(URLQueryItem(name: "tags", value: tags.joined(separator: ",")))
    }
    if command == "update" {
        guard let id = request.existingID, !id.isEmpty else {
            throw BridgeError.invalidRequest("missing Things identifier")
        }
        items.append(URLQueryItem(name: "id", value: id))
        items.append(URLQueryItem(name: "auth-token", value: try authorizationToken()))
        if request.canceled == true { items.append(URLQueryItem(name: "canceled", value: "true")) }
    }
    guard let callback = request.callbackURL, !callback.isEmpty else {
        throw BridgeError.invalidRequest("missing local callback URL")
    }
    items.append(URLQueryItem(name: "x-success", value: callback + "?status=success"))
    items.append(URLQueryItem(name: "x-error", value: callback + "?status=error"))
    items.append(URLQueryItem(name: "x-cancel", value: callback + "?status=cancel"))
    components.queryItems = items
    guard let url = components.url else { throw BridgeError.invalidURL }
    return url
}

func run() throws {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let request = try JSONDecoder().decode(Request.self, from: data)
    if request.action == "permission" {
        _ = try authorizationToken()
        try writeJSON(["status": "keychain-ready"])
        return
    }
    let url = try buildURL(request)
    guard NSWorkspace.shared.open(url) else { throw BridgeError.dispatchFailed }
    try writeJSON(["status": "dispatched"])
}

do {
    try run()
} catch {
    FileHandle.standardError.write(Data("woon-things-url-bridge: \(error.localizedDescription)\n".utf8))
    exit(1)
}
