function apiHeaders(extra) {
    const headers = { 'X-Requested-With': 'XMLHttpRequest' };
    const meta = document.querySelector('meta[name="api-token"]');
    if (meta && meta.content) headers['X-API-Token'] = meta.content;
    return extra ? Object.assign(headers, extra) : headers;
}
