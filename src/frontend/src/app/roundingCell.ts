const KM_PER_DEGREE = 111.32;

/**
 * Where a node that rounds its position can be. Firmware keeps the top `bits`
 * bits of each coordinate in 1e-7 degrees and reports the middle of that cell.
 */
export interface RoundingCell {
  bits: number;
  south: number;
  west: number;
  north: number;
  east: number;
  northSouthKm: number;
  eastWestKm: number;
}

export function roundingCell(
  latitude: number,
  longitude: number,
  bits: number,
): RoundingCell {
  const half = (2 ** (32 - bits) * 1e-7) / 2;
  return {
    bits,
    south: latitude - half,
    west: longitude - half,
    north: latitude + half,
    east: longitude + half,
    northSouthKm: 2 * half * KM_PER_DEGREE,
    eastWestKm: 2 * half * KM_PER_DEGREE * Math.cos((latitude * Math.PI) / 180),
  };
}

/** A position's rounding cell; null when the position is exact. */
export function positionCell(position: {
  latitude: number;
  longitude: number;
  precision_bits: number | null;
}): RoundingCell | null {
  return position.precision_bits === null
    ? null
    : roundingCell(
        position.latitude,
        position.longitude,
        position.precision_bits,
      );
}
