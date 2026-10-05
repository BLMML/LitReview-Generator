// Match the backend's project-title naming policy for every Word download.
export function reviewExportFilename(projectTitle?: string | null): string {
  const cleaned = (projectTitle ?? '')
    .replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, '_')
    .trim()
    .replace(/^\.+|\.+$/g, '')
  const name = Array.from(cleaned)
    .slice(0, 120)
    .join('')
    .replace(/[ .]+$/g, '')
  return `${name || '项目'}文献综述.docx`
}
