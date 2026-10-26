const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

// "12 photos", "1 video", "4 photos · 2 videos"
export function inputSummary({ num_images: photos, num_videos: videos = 0 }) {
  const parts = []
  if (photos || !videos) parts.push(plural(photos, 'photo'))
  if (videos) parts.push(plural(videos, 'video'))
  return parts.join(' · ')
}
