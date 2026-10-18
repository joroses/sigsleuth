import {
  TILE_SIZE,
  decodeTerrariumImage,
  tileId,
  type HeightTile,
  type TileKey,
} from "./lineOfSight";

// Terrain heights from the AWS open-data Terrarium tiles, fetched by the
// browser like the basemaps. Decoded tiles stay in memory while the page is
// open; a failed tile is tried again next time.

export const TERRAIN_TILE_URL =
  "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
export const TERRAIN_ATTRIBUTION =
  'Terrain: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Mapzen Terrain Tiles</a>';
const MAX_PARALLEL = 6;

const decoded = new Map<string, Promise<HeightTile>>();

/** Each tile's heights, or null where it could not be loaded. */
export async function loadTerrain(
  tiles: TileKey[],
): Promise<Map<string, HeightTile>> {
  const queue = [...tiles];
  const result = new Map<string, HeightTile>();
  const worker = async () => {
    for (let tile = queue.shift(); tile !== undefined; tile = queue.shift()) {
      result.set(tileId(tile), await loadTile(tile));
    }
  };
  await Promise.all(Array.from({ length: MAX_PARALLEL }, worker));
  return result;
}

function loadTile(tile: TileKey): Promise<HeightTile> {
  const id = tileId(tile);
  let pending = decoded.get(id);
  if (pending === undefined) {
    pending = fetchTile(tile).catch(() => {
      decoded.delete(id);
      return null;
    });
    decoded.set(id, pending);
  }
  return pending;
}

async function fetchTile({ z, x, y }: TileKey): Promise<Float32Array> {
  const url = TERRAIN_TILE_URL.replace("{z}", String(z))
    .replace("{x}", String(x))
    .replace("{y}", String(y));
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Terrain tile ${response.status}`);
  const image = await createImageBitmap(await response.blob());
  const canvas = document.createElement("canvas");
  canvas.width = TILE_SIZE;
  canvas.height = TILE_SIZE;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) throw new Error("No canvas");
  context.drawImage(image, 0, 0);
  image.close();
  return decodeTerrariumImage(
    context.getImageData(0, 0, TILE_SIZE, TILE_SIZE).data,
  );
}
