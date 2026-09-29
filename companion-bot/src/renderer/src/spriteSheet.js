import sheet from '../../../assets/bot_spritesheet.json'
import { config } from '../../shared/config'

// Resolve the PNG named inside the JSON ("image" field) rather than importing a
// fixed filename, so swapping the sheet only means replacing the two assets.
const pngUrls = import.meta.glob('../../../assets/*.png', {
  eager: true,
  query: '?url',
  import: 'default'
})
const imageUrl = pngUrls[`../../../assets/${sheet.image}`]

export { sheet }

export function fpsFor(state) {
  return config.fps[state] ?? config.defaultFps
}

// Top-left corner (in sheet pixels) of frame `index` of `state`. Frames that
// don't fit in one row wrap onto the next, so any column count works.
export function frameOrigin(state, index) {
  const { row } = sheet.states[state]
  return {
    sx: (index % sheet.columns) * sheet.frameWidth,
    sy: (row + Math.floor(index / sheet.columns)) * sheet.frameHeight
  }
}

// Loads the sheet image and, if possible, a copy of its alpha channel for
// hit-testing. Resolves to { image, alpha } where alpha may be null.
export async function loadSheet() {
  if (!imageUrl) throw new Error(`Sprite image "${sheet.image}" not found in assets/`)
  const image = new Image()
  image.src = imageUrl
  await image.decode()

  let alpha = null
  try {
    const canvas = document.createElement('canvas')
    canvas.width = image.naturalWidth
    canvas.height = image.naturalHeight
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    ctx.drawImage(image, 0, 0)
    // getImageData throws if the canvas is "tainted" (image treated as
    // cross-origin). In that case we fall back to a circular hit area.
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
    alpha = new Uint8Array(canvas.width * canvas.height)
    for (let i = 0; i < alpha.length; i++) alpha[i] = data[i * 4 + 3]
  } catch (err) {
    console.warn('Alpha hit-testing unavailable, using circular hit area:', err)
  }
  return { image, alpha, width: image.naturalWidth }
}

// Is point (x, y), in frame pixels, on an opaque part of this frame?
export function isOpaqueAt(loaded, state, index, x, y, flipped = false) {
  const { frameWidth: fw, frameHeight: fh } = sheet
  if (x < 0 || y < 0 || x >= fw || y >= fh) return false
  if (flipped) x = fw - 1 - x

  if (!loaded.alpha) {
    const r = fw * config.fallbackHitRadius
    return (x - fw / 2) ** 2 + (y - fh / 2) ** 2 <= r * r
  }
  const { sx, sy } = frameOrigin(state, index)
  return loaded.alpha[(sy + y) * loaded.width + (sx + x)] > config.alphaThreshold
}
