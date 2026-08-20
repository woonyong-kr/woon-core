#!/usr/bin/env swift
// Read Apple Calendar event summaries into a private local Markdown projection.

import EventKit
import Foundation

struct Request: Decodable {
    let startAt: String
    let endAt: String
}

struct EventRecord: Encodable {
    let source_event_id: String
    let calendar_name: String
    let title: String
    let start_at: String
    let end_at: String
    let all_day: Bool
    let category_id: String?
}

struct Response: Encodable {
    let events: [EventRecord]
}

enum ExportError: Error, LocalizedError {
    case permissionDenied
    case invalidRequest(String)

    var errorDescription: String? {
        switch self {
        case .permissionDenied: return "EventKit full calendar access is required"
        case .invalidRequest(let message): return message
        }
    }
}

func parseDate(_ value: String) throws -> Date {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) { return date }
    formatter.formatOptions = [.withInternetDateTime]
    guard let date = formatter.date(from: value) else {
        throw ExportError.invalidRequest("invalid ISO8601 date")
    }
    return date
}

func iso(_ value: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: value)
}

func categoryID(from notes: String?) -> String? {
    guard let notes else { return nil }
    let lines = notes.split(separator: "\n", omittingEmptySubsequences: false)
    guard lines.first == "Woon이 생성한 시간 일정입니다." else { return nil }
    let prefix = "Woon category: "
    guard let categoryLine = lines.dropFirst().first(where: { $0.hasPrefix(prefix) }) else {
        return nil
    }
    let category = String(categoryLine.dropFirst(prefix.count))
    let allowed: Set<String> = ["career", "learning", "creative", "life", "relationship", "health", "admin"]
    return allowed.contains(category) ? category : nil
}

func run() throws {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let request = try JSONDecoder().decode(Request.self, from: data)
    let store = EKEventStore()
    guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
        throw ExportError.permissionDenied
    }
    let start = try parseDate(request.startAt)
    let end = try parseDate(request.endAt)
    guard end > start else { throw ExportError.invalidRequest("end must follow start") }
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: predicate).compactMap { event -> EventRecord? in
        guard let title = event.title, !title.isEmpty else { return nil }
        let identifier = event.eventIdentifier ?? event.calendarItemIdentifier
        guard !identifier.isEmpty else { return nil }
        return EventRecord(
            source_event_id: identifier,
            calendar_name: event.calendar.title,
            title: title,
            start_at: iso(event.startDate),
            end_at: iso(event.endDate),
            all_day: event.isAllDay,
            category_id: categoryID(from: event.notes)
        )
    }
    let output = try JSONEncoder().encode(Response(events: events))
    FileHandle.standardOutput.write(output)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

do {
    try run()
} catch {
    FileHandle.standardError.write(Data("woon-calendar-export: \(error.localizedDescription)\n".utf8))
    exit(1)
}
