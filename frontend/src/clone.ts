// DTOs contain only JSON values. JSON cloning also unwraps Vue's reactive proxies.
export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
