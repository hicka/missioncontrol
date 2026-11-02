import Foundation

/// A server payload. The workspace reads dozens of shapes, and a field the server adds
/// or leaves null must never blank a screen, so reading a missing key is not an error:
/// it is `.null`, and the accessors below give an empty value for it.
///
/// Fields read as properties (`message.text.str`, `item.meta.to.str`). The accessors are
/// named so they cannot shadow a server field; use `json["list"]` if one ever does.
@dynamicMemberLookup
enum JSON: Sendable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    init(any value: Any?) {
        switch value {
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() { self = .bool(number.boolValue) } else { self = .number(number.doubleValue) }
        case let string as String: self = .string(string)
        case let array as [Any]: self = .array(array.map { JSON(any: $0) })
        case let object as [String: Any]: self = .object(object.mapValues { JSON(any: $0) })
        default: self = .null
        }
    }

    init(data: Data) {
        self.init(any: try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]))
    }

    subscript(dynamicMember key: String) -> JSON { self[key] }
    subscript(key: String) -> JSON {
        if case .object(let object) = self { return object[key] ?? .null }
        return .null
    }
    subscript(index: Int) -> JSON {
        if case .array(let array) = self, array.indices.contains(index) { return array[index] }
        return .null
    }

    var isNull: Bool { self == .null }
    /// The text of a string or number; empty for anything else.
    var str: String { optStr ?? "" }
    var optStr: String? {
        switch self {
        case .string(let string): return string
        case .number(let number): return number == number.rounded() && abs(number) < 1e15 ? String(Int(number)) : String(number)
        default: return nil
        }
    }
    var num: Double { optNum ?? 0 }
    var optNum: Double? {
        switch self {
        case .number(let number): return number
        case .string(let string): return Double(string)
        case .bool(let bool): return bool ? 1 : 0
        default: return nil
        }
    }
    var int: Int { Int(num) }
    /// Booleans, and the 0/1 integers some stores use for them.
    var flag: Bool {
        switch self {
        case .bool(let bool): return bool
        case .number(let number): return number != 0
        default: return false
        }
    }
    var list: [JSON] { if case .array(let array) = self { return array }; return [] }
    var dict: [String: JSON] { if case .object(let object) = self { return object }; return [:] }
    /// A plain Foundation value, to send back to the server.
    var raw: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let bool): return bool
        case .number(let number): return number
        case .string(let string): return string
        case .array(let array): return array.map(\.raw)
        case .object(let object): return object.mapValues(\.raw)
        }
    }
}

extension JSON: Identifiable {
    /// Rows from the server carry integer or text ids; lists can use `ForEach(rows)`.
    public var id: String { self["id"].str }
}
