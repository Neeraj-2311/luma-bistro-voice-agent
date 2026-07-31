import { Button } from '@/components/ui/button';

/** A place setting: plate, fork, knife. */
function WelcomeImage() {
  return (
    <svg
      width="64"
      height="64"
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      xmlns="http://www.w3.org/2000/svg"
      className="text-fg0 mb-4 size-16"
    >
      <circle cx="32" cy="32" r="15" />
      <circle cx="32" cy="32" r="9" />
      <path d="M9 12v10a4 4 0 0 0 4 4 4 4 0 0 0 4-4V12M13 26v26" />
      <path d="M55 12c-3 3-4 7-4 11v5h-6v-5c0-4 1-8 4-11M51 28v24" />
    </svg>
  );
}

interface WelcomeViewProps {
  startButtonText: string;
  onStartCall: () => void;
}

export const WelcomeView = ({
  startButtonText,
  onStartCall,
  ref,
}: React.ComponentProps<'div'> & WelcomeViewProps) => {
  return (
    <div ref={ref}>
      <section className="bg-background flex flex-col items-center justify-center text-center">
        <WelcomeImage />

        <p className="text-foreground max-w-prose pt-1 leading-6 font-medium">
          Speak to Ava, our reservations host
        </p>

        <Button
          size="lg"
          onClick={onStartCall}
          className="mt-6 w-64 rounded-full font-mono text-xs font-bold tracking-wider uppercase"
        >
          {startButtonText}
        </Button>
      </section>

      <div className="fixed bottom-5 left-0 flex w-full items-center justify-center">
        <p className="text-muted-foreground max-w-prose pt-1 text-xs leading-5 font-normal text-pretty md:text-sm">
          Open Tuesday to Sunday, 5–10 PM. Book, change, or cancel a table by voice.
        </p>
      </div>
    </div>
  );
};
