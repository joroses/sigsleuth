import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { encode } from "uqr";

import { formatNodeId } from "./nodeDisplay";
import { Icon, IconButton } from "./ui";

/**
 * Opens the node's contact link with its QR code, which adds the node to a
 * Meshtastic app.
 */
export function AddContactButton({
  url,
  name,
  nodeId,
}: {
  url: string;
  name: string;
  nodeId: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        <Icon name="contact" />
        Add contact
      </button>
      {/* In the body, so it takes no styles or landmark from the button's place. */}
      {open
        ? createPortal(
            <ContactDialog
              url={url}
              name={name}
              nodeId={nodeId}
              onClose={() => setOpen(false)}
            />,
            document.body,
          )
        : null}
    </>
  );
}

function ContactDialog({
  url,
  name,
  nodeId,
  onClose,
}: {
  url: string;
  name: string;
  nodeId: number;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const copyButton = useRef<HTMLButtonElement>(null);
  // A click closes the dialog outside its box only when it also started
  // there, so a text selection dragged out of the link field keeps it open.
  const pressedOutside = useRef(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    dialog.current?.showModal();
    copyButton.current?.focus();
  }, []);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      // The clipboard API needs HTTPS; over plain HTTP, copy the selection.
      field.current?.select();
      setCopied(document.execCommand("copy"));
    }
  }

  return (
    <dialog
      ref={dialog}
      className="contact-dialog"
      aria-labelledby="contact-dialog-title"
      onClose={onClose}
      onPointerDown={(event) => {
        pressedOutside.current = event.target === event.currentTarget;
      }}
      onClick={(event) => {
        if (pressedOutside.current && event.target === event.currentTarget) {
          event.currentTarget.close();
        }
      }}
    >
      <div className="contact-dialog-body">
        <header className="contact-dialog-header">
          <div>
            <h2 id="contact-dialog-title">Add contact</h2>
            <p>
              {name} <span className="mono muted">{formatNodeId(nodeId)}</span>
            </p>
          </div>
          <IconButton
            icon="close"
            label="Close"
            onClick={() => dialog.current?.close()}
          />
        </header>
        <QrCode text={url} />
        <p className="contact-dialog-hint">
          Scan it with your phone, or open the link there, to add this node to
          the Meshtastic app.
        </p>
        <div className="contact-link">
          <input
            ref={field}
            className="mono"
            value={url}
            readOnly
            spellCheck={false}
            aria-label="Contact link"
            onFocus={(event) => event.currentTarget.select()}
          />
          <button
            ref={copyButton}
            type="button"
            className={copied ? "is-copied" : undefined}
            onClick={() => void copy()}
          >
            <Icon name={copied ? "check" : "copy"} />
            <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
          </button>
        </div>
        <a className="contact-open" href={url} target="_blank" rel="noreferrer">
          Open link
          <Icon name="external" />
        </a>
      </div>
    </dialog>
  );
}

/** Dark on white with a quiet zone, which phone cameras need on a dark page. */
function QrCode({ text }: { text: string }) {
  const { size, data } = encode(text, { border: 4 });
  // One rectangle per run of dark modules in a row.
  let path = "";
  data.forEach((row, y) => {
    for (let x = 0; x < size; x++) {
      if (!row[x]) continue;
      const start = x;
      while (row[x + 1]) x++;
      path += `M${start} ${y}h${x - start + 1}v1H${start}z`;
    }
  });
  return (
    <svg
      className="contact-qr"
      viewBox={`0 0 ${size} ${size}`}
      shapeRendering="crispEdges"
      role="img"
      aria-label="QR code of the contact link"
    >
      <path d={path} fill="#000" />
    </svg>
  );
}
