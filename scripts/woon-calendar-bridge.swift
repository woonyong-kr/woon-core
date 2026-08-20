#!/usr/bin/env swift
// Approval-gated EventKit bridge. Input/output are JSON on standard streams.
// It only reads or writes the pre-created local "Woon 일정" calendar.

import EventKit
import Foundation

struct Request: Decodable {
    let action: String
    let calendarName: String
    let targetCalendarName: String?
    let title: String?
    let startAt: String?
    let endAt: String?
    let existingID: String?
    let categoryID: String?
    let location: String?
    let notes: String?
}

enum BridgeError: Error, LocalizedError {
    case invalidRequest(String)
    case permissionDenied
    case calendarNotFound(String)
    case calendarAmbiguous(String)
    case calendarRenameTargetExists(String)
    case eventNotFound(String)
    case nonWoonEvent
    case verificationMismatch

    var errorDescription: String? {
        switch self {
        case .invalidRequest(let message): return message
        case .permissionDenied: return "EventKit full calendar access is required"
        case .calendarNotFound(let name): return "calendar not found: \(name)"
        case .calendarAmbiguous(let name): return "calendar is ambiguous: \(name)"
        case .calendarRenameTargetExists(let name): return "calendar rename target already exists: \(name)"
        case .eventNotFound(let id): return "event not found: \(id)"
        case .nonWoonEvent: return "event is not owned by Woon 일정"
        case .verificationMismatch: return "saved event does not match the requested values"
        }
    }
}

func writeJSON(_ value: [String: String]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func allowed(_ status: EKAuthorizationStatus) -> Bool {
    return status == .fullAccess
}

func parseDate(_ value: String?) throws -> Date {
    guard let value else { throw BridgeError.invalidRequest("missing event date") }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) { return date }
    formatter.formatOptions = [.withInternetDateTime]
    guard let date = formatter.date(from: value) else {
        throw BridgeError.invalidRequest("invalid ISO8601 date")
    }
    return date
}

func targetCalendar(_ store: EKEventStore, _ name: String) throws -> EKCalendar {
    let matches = store.calendars(for: .event).filter {
        $0.title == name && $0.allowsContentModifications
    }
    if matches.isEmpty { throw BridgeError.calendarNotFound(name) }
    if matches.count != 1 { throw BridgeError.calendarAmbiguous(name) }
    return matches[0]
}

func ownedEvent(_ store: EKEventStore, _ calendar: EKCalendar, _ id: String) throws -> EKEvent {
    guard let event = store.event(withIdentifier: id) else {
        throw BridgeError.eventNotFound(id)
    }
    guard event.calendar.calendarIdentifier == calendar.calendarIdentifier else {
        throw BridgeError.nonWoonEvent
    }
    return event
}

func renameOwnedCalendar(
    _ store: EKEventStore,
    legacyName: String,
    targetName: String,
    eventID: String
) throws {
    guard let event = store.event(withIdentifier: eventID) else {
        throw BridgeError.eventNotFound(eventID)
    }
    guard let calendar = event.calendar else {
        throw BridgeError.nonWoonEvent
    }
    if calendar.title == targetName {
        return
    }
    guard calendar.title == legacyName && calendar.allowsContentModifications else {
        throw BridgeError.nonWoonEvent
    }
    let targetExists = store.calendars(for: .event).contains {
        $0.calendarIdentifier != calendar.calendarIdentifier && $0.title == targetName
    }
    if targetExists {
        throw BridgeError.calendarRenameTargetExists(targetName)
    }
    calendar.title = targetName
    try store.saveCalendar(calendar, commit: true)
}

func verifySavedEvent(_ store: EKEventStore, _ calendar: EKCalendar, _ request: Request, _ id: String) throws {
    let event = try ownedEvent(store, calendar, id)
    let expectedStart = try parseDate(request.startAt)
    let expectedEnd = try parseDate(request.endAt)
    guard event.title == request.title,
          event.startDate == expectedStart,
          event.endDate == expectedEnd,
          event.location == request.location,
          event.notes == request.notes else {
        throw BridgeError.verificationMismatch
    }
}

func storedCategoryID(from notes: String?) -> String? {
    guard let notes else { return nil }
    let lines = notes.split(separator: "\n", omittingEmptySubsequences: false)
    guard lines.first == "Woon이 생성한 시간 일정입니다." else { return nil }
    let prefix = "Woon category: "
    guard let categoryLine = lines.dropFirst().first(where: { $0.hasPrefix(prefix) }) else {
        return nil
    }
    let value = String(categoryLine.dropFirst(prefix.count))
    return isCategoryID(value) ? value : nil
}

func isCategoryID(_ value: String) -> Bool {
    return value.range(of: #"^[a-z][a-z0-9-]{0,31}$"#, options: .regularExpression) != nil
}

func notesWithCategory(_ notes: String?, _ categoryID: String) -> String {
    let marker = "Woon이 생성한 시간 일정입니다."
    let categoryMarker = "Woon category: \(categoryID)"
    guard let notes, !notes.isEmpty else {
        return "\(marker)\n\(categoryMarker)"
    }
    var lines = notes.components(separatedBy: "\n")
    if lines.first == marker {
        if let index = lines.firstIndex(where: { $0.hasPrefix("Woon category: ") }) {
            lines[index] = categoryMarker
        } else {
            lines.insert(categoryMarker, at: 1)
        }
        return lines.joined(separator: "\n")
    }
    return "\(marker)\n\(categoryMarker)\n\n\(notes)"
}

func run() throws {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let request = try JSONDecoder().decode(Request.self, from: data)
    let store = EKEventStore()
    guard allowed(EKEventStore.authorizationStatus(for: .event)) else {
        throw BridgeError.permissionDenied
    }
    if request.action == "permission" {
        try writeJSON(["status": "granted"])
        return
    }

    if request.action == "rename-owned-calendar" {
        guard let targetName = request.targetCalendarName, !targetName.isEmpty else {
            throw BridgeError.invalidRequest("missing calendar rename target")
        }
        guard let eventID = request.existingID, !eventID.isEmpty else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        try renameOwnedCalendar(
            store,
            legacyName: request.calendarName,
            targetName: targetName,
            eventID: eventID
        )
        try writeJSON(["calendar_event_id": eventID, "calendar_name": targetName])
        return
    }

    let calendar = try targetCalendar(store, request.calendarName)
    switch request.action {
    case "create-or-update":
        let event: EKEvent
        if let id = request.existingID {
            event = try ownedEvent(store, calendar, id)
        } else {
            event = EKEvent(eventStore: store)
            event.calendar = calendar
        }
        guard let title = request.title, !title.isEmpty else {
            throw BridgeError.invalidRequest("missing event title")
        }
        event.title = title
        event.startDate = try parseDate(request.startAt)
        event.endDate = try parseDate(request.endAt)
        event.location = request.location
        event.notes = request.notes
        try store.save(event, span: .thisEvent, commit: true)
        guard let eventID = event.eventIdentifier, !eventID.isEmpty else {
            throw BridgeError.invalidRequest("EventKit did not return an event identifier")
        }
        try writeJSON(["calendar_event_id": eventID])
    case "verify":
        guard let id = request.existingID, !id.isEmpty else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        try verifySavedEvent(store, calendar, request, id)
        try writeJSON([
            "calendar_event_id": id,
            "calendar_name": calendar.title,
            "status": "verified"
        ])
    case "cancel":
        guard let id = request.existingID else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        let event = try ownedEvent(store, calendar, id)
        try store.remove(event, span: .thisEvent, commit: true)
        try writeJSON(["calendar_event_id": id])
    case "verify-absent":
        guard let id = request.existingID, !id.isEmpty else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        guard store.event(withIdentifier: id) == nil else {
            throw BridgeError.verificationMismatch
        }
        try writeJSON(["calendar_event_id": id, "status": "absent"])
    case "set-category":
        guard let id = request.existingID, !id.isEmpty else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        guard let categoryID = request.categoryID, isCategoryID(categoryID) else {
            throw BridgeError.invalidRequest("invalid calendar category")
        }
        let event = try ownedEvent(store, calendar, id)
        event.notes = notesWithCategory(event.notes, categoryID)
        try store.save(event, span: .thisEvent, commit: true)
        try writeJSON(["calendar_event_id": id])
    case "verify-category":
        guard let id = request.existingID, !id.isEmpty else {
            throw BridgeError.invalidRequest("missing event identifier")
        }
        guard let categoryID = request.categoryID, isCategoryID(categoryID) else {
            throw BridgeError.invalidRequest("invalid calendar category")
        }
        let event = try ownedEvent(store, calendar, id)
        guard storedCategoryID(from: event.notes) == categoryID else {
            throw BridgeError.verificationMismatch
        }
        try writeJSON([
            "calendar_event_id": id,
            "calendar_name": calendar.title,
            "category_id": categoryID,
            "status": "verified"
        ])
    default:
        throw BridgeError.invalidRequest("unsupported action")
    }
}

func runSelfTest() throws {
    let original = "기존 메모"
    let categorized = notesWithCategory(original, "learning")
    guard storedCategoryID(from: categorized) == "learning", categorized.contains(original) else {
        throw BridgeError.verificationMismatch
    }
    let updated = notesWithCategory(categorized, "relationship")
    guard storedCategoryID(from: updated) == "relationship",
          updated.components(separatedBy: "Woon category: ").count == 2,
          !isCategoryID("invalid category") else {
        throw BridgeError.verificationMismatch
    }
    try writeJSON(["status": "passed"])
}

do {
    if CommandLine.arguments.contains("--self-test") {
        try runSelfTest()
    } else {
        try run()
    }
} catch {
    FileHandle.standardError.write(Data("woon-calendar-bridge: \(error.localizedDescription)\n".utf8))
    exit(1)
}
