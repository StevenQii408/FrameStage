export function readJobIds(search: string): string[] {
  const params = new URLSearchParams(search)
  const multiple = params.get('job_ids')
  const raw = multiple ? multiple.split(',') : [params.get('job_id') || '']
  return [...new Set(raw.map((id) => id.trim()).filter(Boolean))]
}

export function writeJobIds(currentUrl: string, jobIds: string[]): string {
  const url = new URL(currentUrl)
  url.searchParams.delete('job_id')
  if (jobIds.length) url.searchParams.set('job_ids', jobIds.join(','))
  else url.searchParams.delete('job_ids')
  return `${url.pathname}${url.search}${url.hash}`
}
