import { useEffect, useMemo, useState } from "react";

import {
  MAP_NODE_LIMIT,
  hasReportedPosition,
  type HeardPositionList,
  type NodePosition,
  type ReportedNode,
  type MapNodeList,
  fetchMapNode,
  linkPositionsUrl,
  mapNodesUrl,
} from "./api";
import { LineOfSightMap } from "./LineOfSightMap";
import { LineOfSightProfile } from "./LineOfSightProfile";
import {
  TIER_COLORS,
  TIER_LABELS,
  type EndHeight,
  type Profile,
  type SiteSearch,
  ASSUMED_ANTENNA_HEIGHT_M,
  SITE_SLACK_M,
  buildProfile,
  compassPoint,
  findSite,
  formatDistance,
  formatMeters,
  formatPercent,
  groundHeight,
  needsSite,
  planPath,
  positionRoundingKm,
  siteRadiusKm,
  siteTiles,
} from "./lineOfSight";
import {
  formatNodeId,
  lineOfSightHref,
  nodeLabel,
  nodeMarkerLabel,
} from "./nodeDisplay";
import { NodeSearch } from "./NodeSearch";
import { loadTerrain } from "./terrain";
import { useApi } from "./useApi";
import { IconButton, NodeLink, StatusPanel, Timestamp } from "./ui";

type Picked = [number | null, number | null];
const END_LABELS = ["A", "B"] as const;

/**
 * The line-of-sight tab: two nodes, their terrain profile, and the path on a
 * map. With `at`, a time in ms (a Links row's last heard), the pair from the
 * URL sits where its own positions placed it then; picking another node
 * places both where they are now.
 */
export function LineOfSightView({
  pair,
  at = null,
}: {
  pair: [number, number] | null;
  at?: number | null;
}) {
  const { data, error } = useApi<MapNodeList>(mapNodesUrl({}));
  // Line of sight needs the nodes' own positions, not estimates.
  const nodes = useMemo(
    () => (data?.items ?? []).filter(hasReportedPosition),
    [data],
  );
  const byId = useMemo(
    () => new Map(nodes.map((node) => [node.node_id, node])),
    [nodes],
  );
  // One end may be picked before the other; the hash names the pair once both
  // are. Going back to the tab without a pair clears a finished pair but keeps
  // a single picked end.
  const [picked, setPicked] = useState<Picked>(pair ?? [null, null]);
  const pairKey = pair?.join("-") ?? "";
  const [seenPair, setSeenPair] = useState(pairKey);
  if (pairKey !== seenPair) {
    setSeenPair(pairKey);
    if (pair !== null) setPicked(pair);
    else if (picked[0] !== null && picked[1] !== null) setPicked([null, null]);
  }
  const fetched = useMissingNodes(data === null ? null : byId, picked);
  const [hovered, setHovered] = useState<number | null>(null);

  const thenUrl =
    at !== null &&
    pair !== null &&
    picked[0] === pair[0] &&
    picked[1] === pair[1]
      ? // Either order is the same lookup, so a swap keeps it.
        linkPositionsUrl(Math.min(...pair), Math.max(...pair), at)
      : null;
  const then = useApi<HeardPositionList>(thenUrl);

  // Picking another node places both where they are now; swapping the
  // pair keeps its time.
  const choose = (next: Picked, keepTime = false) => {
    setPicked(next);
    const time =
      keepTime && thenUrl !== null && at !== null
        ? new Date(at).toISOString()
        : undefined;
    openHash(
      next[0] !== null && next[1] !== null
        ? lineOfSightHref(next[0], next[1], time)
        : "#links/line-of-sight",
    );
  };

  const find = (nodeId: number | null) =>
    nodeId === null
      ? null
      : (byId.get(nodeId) ?? fetched.get(nodeId)?.node ?? null);
  const nowA = find(picked[0]);
  const nowB = find(picked[1]);
  const heard = then.data?.items;
  const a = useMemo(() => placedThen(nowA, heard), [nowA, heard]);
  const b = useMemo(() => placedThen(nowB, heard), [nowB, heard]);
  const same = picked[0] !== null && picked[0] === picked[1];
  const profile = useProfile(same ? null : a, same ? null : b);

  if (error !== null) return <StatusPanel tone="error">{error}</StatusPanel>;
  if (data === null || then.loading) {
    return <StatusPanel>Loading nodes…</StatusPanel>;
  }
  const unplaced = picked.filter(
    (nodeId) => nodeId !== null && fetched.get(nodeId)?.node === null,
  );
  return (
    <div className="los-page">
      <div className="los-pickers">
        {END_LABELS.map((label, end) => (
          <EndPicker
            key={label}
            label={label}
            nodeId={picked[end]}
            node={end === 0 ? a : b}
            nodes={nodes}
            onPick={(nodeId) =>
              choose(end === 0 ? [nodeId, picked[1]] : [picked[0], nodeId])
            }
          />
        ))}
        <button
          type="button"
          className="text-button"
          onClick={() => choose([picked[1], picked[0]], true)}
          disabled={picked[0] === null && picked[1] === null}
        >
          ⇄ Swap
        </button>
      </div>
      {data.truncated ? (
        <p className="muted">
          Search covers only the {MAP_NODE_LIMIT.toLocaleString()} most recently
          seen nodes with a position.
        </p>
      ) : null}
      {unplaced.length > 0 ? (
        <p className="muted">
          {unplaced.map((nodeId) => formatNodeId(nodeId!)).join(" and ")}{" "}
          {unplaced.length === 1 ? "has" : "have"} no known position, so there
          is no line of sight to show.
        </p>
      ) : null}
      {at !== null && heard !== undefined && heard.length > 0 ? (
        <p className="muted">
          Where they were when last heard together,{" "}
          <Timestamp value={new Date(at).toISOString()} />, by their own
          positions.
        </p>
      ) : null}
      {then.error !== null ? (
        <p className="error-panel compact" role="alert">
          Could not load where they were when last heard; showing where they are
          now.
        </p>
      ) : null}
      {same ? <p className="muted">Pick two different nodes.</p> : null}
      {a !== null && b !== null && !same ? (
        profile === null ? (
          <StatusPanel>Loading terrain…</StatusPanel>
        ) : (
          <>
            <Summary profile={profile} a={a} b={b} />
            <div className="los-body">
              <LineOfSightMap
                a={profile.a.search?.site ?? a.position}
                b={profile.b.search?.site ?? b.position}
                profile={profile}
                hovered={hovered}
                onHover={setHovered}
              />
              <LineOfSightProfile
                profile={profile}
                hovered={hovered}
                onHover={setHovered}
              />
            </div>
          </>
        )
      ) : a === null || b === null ? (
        <p className="muted">
          Pick two nodes with a known position to see the terrain between them.
        </p>
      ) : null}
      <p className="muted los-caveat">
        Rough estimate: reported positions, no buildings. Terrain: AWS Terrain
        Tiles.
      </p>
    </div>
  );
}

/** The node moved to where it was then, when its own positions tell. */
function placedThen(
  node: ReportedNode | null,
  heard: HeardPositionList["items"] | undefined,
): ReportedNode | null {
  const then = heard?.find((item) => item.node_id === node?.node_id);
  if (node === null || then === undefined) return node;
  return {
    ...node,
    position: {
      ...node.position,
      latitude: then.latitude,
      longitude: then.longitude,
      altitude_meters: then.altitude_meters,
    },
  };
}

/**
 * Picked nodes the map list leaves out, fetched one by one; node is null once
 * the node turns out to have no valid position.
 */
function useMissingNodes(
  byId: ReadonlyMap<number, ReportedNode> | null,
  picked: Picked,
): ReadonlyMap<number, { node: ReportedNode | null }> {
  const [fetched, setFetched] = useState<
    ReadonlyMap<number, { node: ReportedNode | null }>
  >(new Map());
  const missing = picked.filter(
    (nodeId): nodeId is number =>
      nodeId !== null && byId !== null && !byId.has(nodeId),
  );
  const missingKey = missing.join("-");
  useEffect(() => {
    if (missingKey === "") return;
    const controller = new AbortController();
    for (const nodeId of missingKey.split("-").map(Number)) {
      fetchMapNode(nodeId, controller.signal)
        .catch(() => null)
        .then((node) => {
          if (controller.signal.aborted) return;
          const reported =
            node !== null && hasReportedPosition(node) ? node : null;
          setFetched((current) =>
            new Map(current).set(nodeId, { node: reported }),
          );
        });
    }
    return () => controller.abort();
  }, [missingKey]);
  return fetched;
}

function openHash(hash: string): void {
  window.location.hash = hash;
}

function EndPicker({
  label,
  nodeId,
  node,
  nodes,
  onPick,
}: {
  label: string;
  nodeId: number | null;
  node: ReportedNode | null;
  nodes: ReportedNode[];
  onPick: (nodeId: number | null) => void;
}) {
  return (
    <div className="los-end">
      <span className="minimap-marker minimap-target los-end-marker">
        {label}
      </span>
      {nodeId === null ? (
        <NodeSearch
          nodes={nodes}
          label={`Node ${label}`}
          onSelect={(picked) => onPick(picked.node_id)}
        />
      ) : (
        <span className="los-chosen">
          {node !== null ? (
            <span className="node-avatar avatar-green">
              {nodeMarkerLabel(node)}
            </span>
          ) : null}
          <NodeLink
            nodeId={nodeId}
            name={node === null ? null : nodeLabel(node)}
          />
          <IconButton
            icon="close"
            label={`Clear node ${label}`}
            onClick={() => onPick(null)}
          />
        </span>
      )}
    </div>
  );
}

// Tiles are fetched in the browser; the profile appears once they are in.
function useProfile(
  a: ReportedNode | null,
  b: ReportedNode | null,
): Profile | null {
  const [result, setResult] = useState<{
    key: string;
    profile: Profile;
  } | null>(null);
  const key = a === null || b === null ? null : `${a.node_id}-${b.node_id}`;
  useEffect(() => {
    if (a === null || b === null || key === null) return;
    let cancelled = false;
    void profileBetween([a.position, b.position]).then((profile) => {
      if (!cancelled) setResult({ key, profile });
    });
    return () => {
      cancelled = true;
    };
  }, [a, b, key]);
  return result?.key === key ? result.profile : null;
}

type Searches = [SiteSearch | null, SiteSearch | null];

/**
 * The profile between two positions. An end whose reported altitude is too
 * high for the ground there is moved to a site matching it, when one is near,
 * which costs a second round of tiles.
 */
async function profileBetween(ends: [NodePosition, NodePosition]) {
  const first = await profileFrom(ends, [null, null]);
  const heights = [first.a, first.b];
  if (!heights.some(needsSite)) return first;
  const searches = (await Promise.all(
    ends.map((end, index) =>
      needsSite(heights[index])
        ? searchSite(end, heights[index].ground!)
        : null,
    ),
  )) as Searches;
  return profileFrom(ends, searches);
}

async function profileFrom(
  ends: [NodePosition, NodePosition],
  searches: Searches,
) {
  const [a, b] = ends.map((end, index) => searches[index]?.site ?? end);
  const plan = planPath(a, b);
  const tiles = await loadTerrain(plan.tiles);
  const grounds = plan.points.map((point) =>
    groundHeight(point, plan.zoom, tiles),
  );
  return buildProfile(
    plan.points,
    grounds,
    ends[0].altitude_meters,
    ends[1].altitude_meters,
    searches,
  );
}

/** A site for the end, or null when its tiles could not be loaded. */
async function searchSite(
  end: NodePosition,
  ground: number,
): Promise<SiteSearch | null> {
  const radiusKm = siteRadiusKm(end);
  const tiles = await loadTerrain(siteTiles(end, radiusKm));
  const site = findSite(
    end,
    end.altitude_meters! - SITE_SLACK_M,
    radiusKm,
    tiles,
  );
  if (site === undefined) return null;
  return {
    from: { latitude: end.latitude, longitude: end.longitude },
    fromGround: ground,
    roundingKm: positionRoundingKm(end),
    radiusKm,
    site,
  };
}

function Summary({
  profile,
  a,
  b,
}: {
  profile: Profile;
  a: ReportedNode;
  b: ReportedNode;
}) {
  const worst = profile.worst;
  const known = worst !== null && Number.isFinite(worst.ratio);
  return (
    <section className="los-summary" aria-label="Line of sight summary">
      <p className="los-verdict">
        {known && worst.tier !== null ? (
          <strong style={{ color: TIER_COLORS[worst.tier] }}>
            {TIER_LABELS[worst.tier]}
          </strong>
        ) : (
          <strong>Terrain unavailable</strong>
        )}{" "}
        over {formatDistance(profile.distance)}
      </p>
      <dl className="los-facts">
        {known ? (
          <div>
            <dt>Worst clearance</dt>
            <dd>
              {formatMeters(worst.clearance)} ({formatPercent(worst.ratio)} of
              the Fresnel radius) at {formatDistance(worst.distance)} from A
            </dd>
          </div>
        ) : null}
        <EndFacts label="A" node={a} end={profile.a} />
        <EndFacts label="B" node={b} end={profile.b} />
      </dl>
      {profile.incomplete ? (
        <p className="error-panel compact" role="alert">
          Terrain unavailable for part of the path.
        </p>
      ) : null}
    </section>
  );
}

function EndFacts({
  label,
  node,
  end,
}: {
  label: string;
  node: ReportedNode;
  end: EndHeight;
}) {
  const search = end.search;
  const searched =
    search === null ? "" : `within ${formatDistance(search.radiusKm * 1000)}`;
  const from =
    end.height === null
      ? "unknown"
      : end.source === "reported"
        ? `${formatMeters(end.height)}, its reported altitude`
        : end.source === "site" && search?.site
          ? `${formatMeters(end.height)}, its reported altitude, on the nearest hill that high: ${formatDistance(search.site.offset)} ${compassPoint(search.from, search.site)} (probably where it really is)`
          : `${formatMeters(end.height)}, ${ASSUMED_ANTENNA_HEIGHT_M} m above the ground (${
              end.ignored === null
                ? "no altitude reported"
                : "reported altitude not used"
            })`;
  // Why a reported altitude was not used, on hover.
  const ignoredWhy =
    end.ignored === null
      ? undefined
      : end.ground !== null && end.ignored < end.ground
        ? `Reported ${formatMeters(end.ignored)} is below the ground`
        : search === null
          ? `Reported ${formatMeters(end.ignored)} is too high for the ground here`
          : `Reported ${formatMeters(end.ignored)} is too high, and no hill that high ${searched}`;
  const ground = search === null ? end.ground : search.fromGround;
  return (
    <div>
      <dt>
        {label} · {nodeLabel(node)}
      </dt>
      <dd>
        Ground {ground === null ? "unknown" : formatMeters(ground)}
        {search !== null && search.roundingKm > 0
          ? ` (position rounded to ~${formatDistance(search.roundingKm * 1000)})`
          : ""}
        ; antenna height <span title={ignoredWhy}>{from}</span>
      </dd>
    </div>
  );
}
