// The server supplies deployment branding before React mounts. Vite development
// and unconfigured deployments keep the upstream identity.
export const appName = document.querySelector<HTMLMetaElement>('meta[name="app-name"]')?.content || 'myc3l1um';
export const appLogoUrl = document.querySelector<HTMLMetaElement>('meta[name="app-logo-url"]')?.content || '';
document.title = appName;
