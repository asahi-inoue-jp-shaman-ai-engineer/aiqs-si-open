// Photo preparation for target registration: client-side
// normalization (long edge ~2048px, JPEG re-encode) plus the image
// size contract, in one place.
//
// ★ Fragile coupling, read before touching the pipeline: the photo
// bytes the client sends are stored on the server as the target's
// profile photo, and the server derives the scan's observer
// coordinate from a thumbnail of THOSE bytes (server:
// profile_image.thumbnail_bytes over the stored photo). Changing
// what bytes leave the client therefore changes future targets'
// coordinates. That is why the tests pin, for a fixed fixture
// image, both the legacy pipeline's payload bytes/hash and the
// normalized pipeline's payload bytes/hash: any future change to
// either pipeline must show up as a deliberate fixture update.
//
// Image size contract — four different units, one per layer. All
// four must hold for a registration to be accepted:
//   1. base64 characters, per photo: at most 7 MiB of characters
//      (7 * 1024 * 1024). Enforced by the server intake
//      (service.register_target / submit_image_scan) on the base64
//      string length.
//   2. decoded bytes, per photo: at most 5 MiB
//      (5 * 1024 * 1024 bytes after base64 decoding). Enforced by
//      the server thumbnail step (profile_image.thumbnail_bytes).
//   3. pixels, per photo: at most 16,000,000 pixels
//      (width * height of the decoded image). Enforced by the same
//      server thumbnail step.
//   4. HTTP body, per registration request: at most 20 MiB
//      (20 * 1024 * 1024 bytes for the whole JSON body — two photos
//      plus fields). Enforced by the request body limit middleware
//      for /api/app/targets.
// Normalizing to a 2048px long edge bounds a photo to at most
// 2048 * 2048 pixels, and a JPEG re-encode at that size lands far
// under units 1 and 2 in practice; the checks below still run on
// the encoded result so an pathological image fails here, with a
// reason, instead of as a server rejection.

export const IMAGE_SIZE_CONTRACT = Object.freeze({
  maxBase64CharsPerPhoto: 7 * 1024 * 1024,
  maxDecodedBytesPerPhoto: 5 * 1024 * 1024,
  maxPixelsPerPhoto: 16_000_000,
  maxBodyBytesPerRequest: 20 * 1024 * 1024,
  normalizedLongEdge: 2048,
  jpegQuality: 0.85,
});

// Approximate decoded byte count of a base64 string (exact when the
// padding is well-formed): every 4 characters carry 3 bytes, minus
// one byte per trailing '='.
export function decodedBytesOfBase64(base64) {
  if (typeof base64 !== 'string' || !base64) return 0;
  const padding = base64.endsWith('==') ? 2 : base64.endsWith('=') ? 1 : 0;
  return Math.max(0, Math.floor(base64.length / 4) * 3 - padding);
}

// Checks one prepared photo against contract units 1–3 (unit 4 is a
// property of the whole request and is bounded by 1–3 holding for
// both photos). Returns a list of violation codes, empty when the
// photo is inside the contract.
export function imageContractViolations({ base64, width, height }) {
  const violations = [];
  if (typeof base64 !== 'string' || !base64) violations.push('empty');
  else {
    if (base64.length > IMAGE_SIZE_CONTRACT.maxBase64CharsPerPhoto) {
      violations.push('base64_too_large');
    }
    if (decodedBytesOfBase64(base64) > IMAGE_SIZE_CONTRACT.maxDecodedBytesPerPhoto) {
      violations.push('decoded_too_large');
    }
  }
  if (typeof width === 'number' && typeof height === 'number'
      && width * height > IMAGE_SIZE_CONTRACT.maxPixelsPerPhoto) {
    violations.push('too_many_pixels');
  }
  return violations;
}

// The scale plan for normalization: shrink so the long edge is at
// most maxEdge, never upscale, keep the aspect ratio, and never
// round a dimension down to zero.
export function planNormalization(width, height, maxEdge = IMAGE_SIZE_CONTRACT.normalizedLongEdge) {
  const longEdge = Math.max(width, height);
  if (!(longEdge > maxEdge)) return { width, height, resized: false };
  const scale = maxEdge / longEdge;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    resized: true,
  };
}

// The legacy pipeline's payload extraction, kept verbatim: a
// FileReader data URL ("data:<mime>;base64,....") contributes
// everything after its first comma. The fixture tests pin what this
// produces for a fixed image, because those bytes are what the
// server thumbnail (and therefore the observer coordinate) was
// built from for every target registered before normalization.
export function legacyPhotoPayload(dataUrl) {
  const text = typeof dataUrl === 'string' ? dataUrl : '';
  return text.includes(',') ? text.slice(text.indexOf(',') + 1) : '';
}

export function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}

// Default browser codec: createImageBitmap applies the EXIF
// orientation (imageOrientation: 'from-image') so phone photos are
// re-encoded upright; the canvas draws at the planned size and
// encodes JPEG. Kept behind this seam so tests can substitute a
// deterministic fixture codec.
export function browserImageCodec(global = globalThis) {
  return {
    async decode(file) {
      if (typeof global.createImageBitmap !== 'function') {
        throw new Error('image decoding unavailable');
      }
      const bitmap = await global.createImageBitmap(file, { imageOrientation: 'from-image' });
      return { bitmap, width: bitmap.width, height: bitmap.height };
    },
    async encode({ bitmap, width, height, quality }) {
      const canvas = global.document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d');
      context.drawImage(bitmap, 0, 0, width, height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (!blob) throw new Error('image encoding failed');
      return blob;
    },
  };
}

// Prepares one photo file for registration. Returns one of:
//   { ok: true, base64, width, height, resized }
//   { ok: false, reason: 'no_file' }        — nothing selected
//   { ok: false, reason: 'decode_failed' }  — the browser could not
//     decode the image (HEIC in browsers without HEIC support,
//     a corrupt file, ...): the caller shows the alternative-format
//     guidance instead of sending bytes the server would reject.
//   { ok: false, reason: 'too_large', violations } — even after
//     normalization the result breaks the size contract.
export async function normalizePhotoFile(file, codec = browserImageCodec()) {
  if (!file) return { ok: false, reason: 'no_file' };
  let decoded;
  try {
    decoded = await codec.decode(file);
  } catch {
    return { ok: false, reason: 'decode_failed' };
  }
  const plan = planNormalization(decoded.width, decoded.height);
  let blob;
  try {
    blob = await codec.encode({
      bitmap: decoded.bitmap, width: plan.width, height: plan.height,
      quality: IMAGE_SIZE_CONTRACT.jpegQuality,
    });
  } catch {
    return { ok: false, reason: 'decode_failed' };
  }
  const base64 = bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
  const violations = imageContractViolations({ base64, width: plan.width, height: plan.height });
  if (violations.length) return { ok: false, reason: 'too_large', violations };
  return { ok: true, base64, width: plan.width, height: plan.height, resized: plan.resized };
}
