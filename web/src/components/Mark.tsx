export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <path fill="var(--accent)" d="M32 3.500 56.500 12v20c0 12-9.500 22.500-24.500 28.500C17 54.500 7.500 44 7.500 32V12z" />
      <path fill="none" stroke="var(--accent-ink)" strokeWidth="7" strokeLinecap="round" strokeLinejoin="round" d="m20 25.750 12 13.250 12-13.250" />
    </svg>
  )
}
