import { Children, cloneElement, isValidElement, useEffect, useId, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import './tooltip.css';

export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <TooltipPrimitive.Provider delayDuration={300} skipDelayDuration={150} disableHoverableContent={false}>
      {children}
    </TooltipPrimitive.Provider>
  );
}

type TriggerProps = {
  disabled?: boolean;
  tabIndex?: number;
  className?: string;
  children?: ReactNode;
  'aria-label'?: string;
  'aria-describedby'?: string;
};

function plainText(value: ReactNode): string {
  return Children.toArray(value)
    .map((child) =>
      typeof child === 'string' || typeof child === 'number'
        ? String(child)
        : isValidElement<TriggerProps>(child)
          ? plainText(child.props.children)
          : '',
    )
    .join(' ')
    .trim();
}

/** A shared hover/focus explanation; existing controls keep their native behavior. */
export default function Tooltip({
  content,
  children,
  mono = false,
  side = 'bottom',
  triggerClassName = '',
}: {
  content?: ReactNode;
  children: ReactElement<TriggerProps>;
  mono?: boolean;
  side?: 'top' | 'right' | 'bottom' | 'left';
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  const body = useRef<HTMLDivElement>(null);
  const hasContent = content !== undefined && content !== null && content !== false && content !== '';
  const disabled = !!children.props.disabled;
  const staticText =
    typeof children.type === 'string' && ['span', 'small', 'th', 'rect'].includes(children.type);

  useEffect(() => {
    if (!hasContent) setOpen(false);
  }, [hasContent]);

  useEffect(() => {
    if (!open) return;
    const dismiss = () => setOpen(false);
    window.addEventListener('blur', dismiss);
    return () => window.removeEventListener('blur', dismiss);
  }, [open]);

  if (!hasContent) return children;

  const trigger = disabled ? (
    <span
      className={`tooltip-disabled-trigger ${triggerClassName}`.trim()}
      tabIndex={0}
      role="group"
      aria-disabled="true"
      aria-label={children.props['aria-label'] || plainText(children.props.children) || plainText(content)}
      aria-describedby={open ? contentId : undefined}
      onClickCapture={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      {children}
    </span>
  ) : (
    cloneElement(children, {
      tabIndex: children.props.tabIndex ?? (staticText || children.type === 'label' ? 0 : undefined),
      'aria-describedby':
        [children.props['aria-describedby'], open ? contentId : ''].filter(Boolean).join(' ') || undefined,
      className:
        [children.props.className, staticText ? 'tooltip-text-trigger' : ''].filter(Boolean).join(' ') ||
        undefined,
    })
  );

  return (
    <TooltipPrimitive.Root open={open && hasContent} onOpenChange={setOpen}>
      <TooltipPrimitive.Trigger
        asChild
        onKeyDown={(event) => {
          // Static detail labels can scroll long descriptions while keeping focus on the label.
          if (!open || !staticText || !body.current || body.current.scrollHeight <= body.current.clientHeight)
            return;
          const amount = ['PageDown', 'PageUp'].includes(event.key) ? body.current.clientHeight * 0.8 : 36;
          if (['ArrowDown', 'PageDown', 'ArrowUp', 'PageUp'].includes(event.key)) {
            event.preventDefault();
            body.current.scrollTop += ['ArrowUp', 'PageUp'].includes(event.key) ? -amount : amount;
          }
        }}
      >
        {trigger}
      </TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          id={contentId}
          className={`app-tooltip ${mono ? 'app-tooltip-mono' : ''}`.trim()}
          side={side}
          sideOffset={8}
          collisionPadding={12}
          avoidCollisions
          hideWhenDetached
          onEscapeKeyDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setOpen(false);
          }}
        >
          <div ref={body} className="app-tooltip-body">
            {content}
          </div>
          <TooltipPrimitive.Arrow className="app-tooltip-arrow" width={10} height={5} />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
