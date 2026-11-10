import { NodeListPage, nodeSearchUrl } from "./api";
import { formatNodeId, nodeLabel } from "./nodeDisplay";
import { useApi } from "./useApi";

/**
 * The name of a node a page holds only the number of; its ID until loaded or
 * when unknown.
 */
export function useNodeName(nodeId: number | null): string | null {
  const id = nodeId === null ? null : formatNodeId(nodeId);
  const { data } = useApi<NodeListPage>(id === null ? null : nodeSearchUrl(id));
  const node = data?.items.find((item) => item.node_id === nodeId);
  return node === undefined ? id : nodeLabel(node);
}
