export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function safeURL(value) { try { const url=new URL(value); return ['https:','http:'].includes(url.protocol)?url.href:'#'; } catch { return '#'; } }
export function formPayload(form, booleans=[],numbers=[]) { const data=Object.fromEntries(form.entries()); for(const key of booleans)data[key]=form.has(key);for(const key of numbers)if(key in data)data[key]=Number(data[key]);return data; }
export const dateText = value => value ? new Date(value).toLocaleString('vi-VN',{timeZone:'Asia/Ho_Chi_Minh',hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}) : 'Chưa có';
export const money = value => '$'+Number(value||0).toFixed(4);
