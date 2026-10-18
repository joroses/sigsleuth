import * as maplibregl from "maplibre-gl";

import { type LinkKind } from "./api";
import { type LatLon } from "./lineOfSight";
import { addLinkLayers, type HalvedLink, linkHalves } from "./mapLinkLayers";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

/**
 * A Links row's two nodes, A and B, with the line the map draws between them:
 * each half in the tier of what the node at its end hears.
 */
export function LinkMinimap({
  linkKind,
  link,
  a,
  b,
}: {
  linkKind: LinkKind;
  link: HalvedLink;
  a: LatLon;
  b: LatLon;
}) {
  const { latitude: aLat, longitude: aLon } = a;
  const { latitude: bLat, longitude: bLon } = b;
  const { container, failure } = useBasemapMap(
    {
      flat: true,
      view: () => ({
        bounds: new maplibregl.LngLatBounds([aLon, aLat], [aLon, aLat]).extend([
          bLon,
          bLat,
        ]),
        fitBoundsOptions: { padding: 60, maxZoom: 13 },
      }),
      onStyleLoad: (map) =>
        addLinkLayers(
          map,
          linkKind,
          {
            type: "FeatureCollection",
            features: linkHalves("link", link, [aLon, aLat], [bLon, bLat]),
          },
          undefined,
          0,
        ),
      setup: (map) => {
        for (const [label, lon, lat] of [
          ["A", aLon, aLat],
          ["B", bLon, bLat],
        ] as const) {
          const element = document.createElement("span");
          element.className = "minimap-marker minimap-target los-end-marker";
          element.textContent = label;
          new maplibregl.Marker({ element }).setLngLat([lon, lat]).addTo(map);
        }
      },
    },
    [linkKind, link, aLat, aLon, bLat, bLon],
  );
  return (
    <>
      {failure !== null ? (
        <p className="muted">{MAP_FAILURE_MESSAGES[failure]}</p>
      ) : null}
      <div
        ref={container}
        className="minimap-canvas"
        hidden={failure !== null}
      />
    </>
  );
}
