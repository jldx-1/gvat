// The local transport is the only GVAT-specific layer below CVAT's own workspace.
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
    sessionStorage.setItem('gvat-token', fragment.get('token')!);
    history.replaceState(null, '', location.pathname);
}
export const token = sessionStorage.getItem('gvat-token') || '';
export let session: any;
export function setSession(value: any): void { session = value; }
export async function api(path: string, body?: unknown, method?: string): Promise<any> {
    const response = await fetch(`/api/cvat/${path}`, {
        method: method || (body === undefined ? 'GET' : 'PUT'),
        headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store',
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Local annotation request failed (${response.status})`);
    return value;
}
