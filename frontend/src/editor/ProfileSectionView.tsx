import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ProfileSection, ProfileSlot } from "../api/hooks/editing.ts";
import { SECONDARY_BUTTON } from "../app/styles.ts";
import { useLanguage } from "../widgets/environment.ts";
import { localized } from "../widgets/vocab.ts";
import { focusSlotFrame, useSlotFocus } from "./slotFocus.ts";
import { widgetFor } from "./widgets/registry.ts";

/** One slot's widget, in a frame that takes the focus when a diagnostic asks for the slot. */
function SlotFrame({ slot }: { slot: ProfileSlot }) {
  const Widget = widgetFor(slot.widget);
  const frame = useRef<HTMLDivElement>(null);
  const { request, done } = useSlotFocus();
  useEffect(() => {
    if (request?.slotId === slot.id && frame.current !== null) {
      focusSlotFrame(frame.current);
      done(request.nonce);
    }
  }, [request, slot.id, done]);
  return (
    // The frame itself is focused only when the slot has no control to focus.
    <div ref={frame} data-slot={slot.id} tabIndex={-1} className="w-full">
      <Widget slot={slot} base={null} />
    </div>
  );
}

function renderSlot(slot: ProfileSlot) {
  return <SlotFrame key={slot.id} slot={slot} />;
}

/**
 * One profile section: its heading and slots. An `advanced` section shows only its heading
 * until "Add details" is pressed; in other sections only the `advanced` slots wait behind it.
 * A slot a diagnostic asks for (`slotFocus.ts`) opens the details it waits behind.
 */
export function ProfileSectionView({ section }: { section: ProfileSection }) {
  const { t } = useTranslation("editor");
  const language = useLanguage();
  const headingId = useId();
  const detailsId = useId();
  const [expanded, setExpanded] = useState(false);
  const advanced = section.advanced === true;
  const visible = advanced ? [] : section.slots.filter((slot) => !slot.advanced);
  const hidden = advanced ? section.slots : section.slots.filter((slot) => slot.advanced);
  const { request } = useSlotFocus();
  const wanted = request !== null && hidden.some((slot) => slot.id === request.slotId);
  useEffect(() => {
    if (wanted) {
      setExpanded(true);
    }
  }, [wanted, request]);
  return (
    <section
      aria-labelledby={headingId}
      data-section={section.id}
      className="flex w-full flex-col gap-3"
    >
      <h2 id={headingId} className="text-xl font-semibold text-slate-900">
        {localized(section.label, language)}
      </h2>
      {visible.map(renderSlot)}
      {hidden.length > 0 && (
        <>
          <div>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              aria-expanded={expanded}
              aria-controls={detailsId}
              onClick={() => setExpanded((open) => !open)}
            >
              {t(expanded ? "profile.hide_details" : "profile.add_details")}
            </button>
          </div>
          <div id={detailsId} hidden={!expanded} className="flex w-full flex-col gap-3">
            {expanded && hidden.map(renderSlot)}
          </div>
        </>
      )}
    </section>
  );
}
