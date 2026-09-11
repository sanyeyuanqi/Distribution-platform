import { Children, Fragment, isValidElement, useId, useRef, useState } from 'react';
import type { ReactNode, SelectHTMLAttributes } from 'react';
import * as SelectPrimitive from '@radix-ui/react-select';
import { Check, ChevronDown, ChevronUp } from 'lucide-react';
import Tooltip from './Tooltip';
import './select.css';

type Option = { value: string; label: string; disabled: boolean };
type Change = { target: { value: string }; currentTarget: { value: string } };
type Props = Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  'onChange' | 'value' | 'defaultValue' | 'multiple' | 'size'
> & {
  value?: string | number;
  defaultValue?: string | number;
  onChange?: (event: Change) => void;
};

function textContent(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) =>
      isValidElement<{ children?: ReactNode }>(child) ? textContent(child.props.children) : String(child),
    )
    .join('');
}

function collectOptions(children: ReactNode): Option[] {
  const options: Option[] = [];
  Children.forEach(children, (child) => {
    if (
      !isValidElement<{ value?: string | number; children?: ReactNode; disabled?: boolean; label?: string }>(
        child,
      )
    )
      return;
    if (child.type === Fragment) options.push(...collectOptions(child.props.children));
    if (child.type === 'option') {
      const label = child.props.label ?? textContent(child.props.children);
      options.push({ value: String(child.props.value ?? label), label, disabled: !!child.props.disabled });
    }
  });
  return options;
}

/** Shared styled menu; native option values and required form behavior are preserved. */
export default function Select({
  value,
  defaultValue,
  onChange,
  children,
  className = '',
  disabled = false,
  required,
  name,
  id,
  title,
  autoFocus,
  form,
  tabIndex,
  'aria-label': ariaLabel,
  'aria-labelledby': labelledBy,
  'aria-describedby': describedBy,
  'aria-invalid': ariaInvalid,
}: Props) {
  const options = collectOptions(children);
  const generatedId = useId();
  let emptyValue = `__empty_option_${generatedId}`;
  while (options.some((option) => option.value === emptyValue)) emptyValue += '_';
  const [localValue, setLocalValue] = useState(String(defaultValue ?? options[0]?.value ?? ''));
  const selected = String(value ?? localValue);
  const selectedOption = options.find((option) => option.value === selected);
  const emptyOption = options.find((option) => option.value === '');
  const [open, setOpen] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  const choose = (next: string) => {
    if (disabled || trigger.current?.matches(':disabled')) return;
    const actual = next === emptyValue ? '' : next;
    if (value === undefined) setLocalValue(actual);
    setInvalid(false);
    if (actual !== selected) onChange?.({ target: { value: actual }, currentTarget: { value: actual } });
  };

  return (
    <div
      className={`select-control ${className}`}
      onInvalidCapture={(event) => {
        // The primitive keeps a native select for form validity; focus the visible control.
        event.preventDefault();
        setInvalid(true);
        const control = event.target as HTMLSelectElement;
        const firstInvalid = control.form
          ? Array.from(control.form.elements).find(
              (element) =>
                (element instanceof HTMLInputElement ||
                  element instanceof HTMLSelectElement ||
                  element instanceof HTMLTextAreaElement) &&
                element.willValidate &&
                !element.validity.valid,
            )
          : control;
        // Every invalid field is marked, but only the first may open its menu.
        setOpen(firstInvalid === control);
        if (firstInvalid === control) trigger.current?.focus();
      }}
    >
      <SelectPrimitive.Root
        value={selected}
        onValueChange={choose}
        open={open && !disabled}
        onOpenChange={(next) => setOpen(next && !trigger.current?.matches(':disabled'))}
        disabled={disabled}
        required={required}
        name={name}
        form={form}
      >
        <Tooltip content={open ? undefined : title} triggerClassName="select-tooltip-trigger">
          <SelectPrimitive.Trigger
            ref={trigger}
            id={id}
            disabled={disabled}
            autoFocus={autoFocus}
            tabIndex={tabIndex}
            className="select-trigger"
            aria-label={ariaLabel}
            aria-labelledby={labelledBy}
            aria-describedby={describedBy}
            aria-invalid={ariaInvalid || invalid || undefined}
          >
            <SelectPrimitive.Value placeholder={emptyOption?.label || '—'}>
              {selectedOption?.label || (selected ? selected : undefined)}
            </SelectPrimitive.Value>
            <SelectPrimitive.Icon className="select-chevron">
              <ChevronDown size={16} />
            </SelectPrimitive.Icon>
          </SelectPrimitive.Trigger>
        </Tooltip>
        <SelectPrimitive.Portal>
          <SelectPrimitive.Content
            className="select-menu"
            position="popper"
            sideOffset={6}
            align="start"
            collisionPadding={12}
            onEscapeKeyDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setOpen(false);
            }}
          >
            <SelectPrimitive.ScrollUpButton className="select-scroll">
              <ChevronUp size={15} />
            </SelectPrimitive.ScrollUpButton>
            <SelectPrimitive.Viewport className="select-viewport">
              {options.map((option) => (
                <SelectPrimitive.Item
                  key={option.value}
                  value={option.value || emptyValue}
                  disabled={option.disabled}
                  className="select-option"
                  textValue={option.label}
                  aria-selected={option.value === selected}
                  data-current={option.value === selected ? '' : undefined}
                >
                  <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                  <span className="select-check" aria-hidden="true">
                    {option.value === selected && <Check size={15} />}
                  </span>
                </SelectPrimitive.Item>
              ))}
              {!options.length && <div className="select-empty">—</div>}
            </SelectPrimitive.Viewport>
            <SelectPrimitive.ScrollDownButton className="select-scroll">
              <ChevronDown size={15} />
            </SelectPrimitive.ScrollDownButton>
          </SelectPrimitive.Content>
        </SelectPrimitive.Portal>
      </SelectPrimitive.Root>
    </div>
  );
}
