export function resolveRows(data, tab) {
  if (!data) return [];
  if (Array.isArray(data[tab])) return data[tab];
  if (Array.isArray(data.events)) return data.events;
  return [];
}

export function createRequestGuard() {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isCurrent(requestId) {
      return requestId === latest;
    },
  };
}
