import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Users,
  Megaphone,
  PhoneCall,
  AudioLines,
  CalendarClock,
  Bot,
  Mic2,
  BookOpen,
  Plus,
  Sparkles,
} from "lucide-react";

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const navigate = useNavigate();

  const go = (to: string) => {
    onOpenChange(false);
    navigate(to);
  };

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Type a command, route, or search…" />
      <CommandList>
        <CommandEmpty>No results.</CommandEmpty>

        <CommandGroup heading="Quick actions">
          <CommandItem onSelect={() => go("/campaigns/new")}>
            <Plus className="mr-2 size-4" />
            New campaign
          </CommandItem>
          <CommandItem onSelect={() => go("/customers")}>
            <Sparkles className="mr-2 size-4 text-[color:var(--ai)]" />
            Ask AI about a customer
          </CommandItem>
          <CommandItem onSelect={() => go("/knowledge")}>
            <BookOpen className="mr-2 size-4" />
            Add knowledge base document
          </CommandItem>
        </CommandGroup>

        <CommandSeparator />

        <CommandGroup heading="Navigate">
          <CommandItem onSelect={() => go("/dashboard")}>
            <LayoutDashboard className="mr-2 size-4" /> Dashboard
          </CommandItem>
          <CommandItem onSelect={() => go("/customers")}>
            <Users className="mr-2 size-4" /> Customers
          </CommandItem>
          <CommandItem onSelect={() => go("/campaigns")}>
            <Megaphone className="mr-2 size-4" /> Campaigns
          </CommandItem>
          <CommandItem onSelect={() => go("/recovery-cases")}>
            <Bot className="mr-2 size-4" /> Recovery Cases
          </CommandItem>
          <CommandItem onSelect={() => go("/callbacks")}>
            <CalendarClock className="mr-2 size-4" /> Callbacks
          </CommandItem>
          <CommandItem onSelect={() => go("/recordings")}>
            <AudioLines className="mr-2 size-4" /> Call Recordings
          </CommandItem>
          <CommandItem onSelect={() => go("/voice-test")}>
            <PhoneCall className="mr-2 size-4" /> AI Voice Test
          </CommandItem>
          <CommandItem onSelect={() => go("/personas")}>
            <Sparkles className="mr-2 size-4" /> Personas
          </CommandItem>
          <CommandItem onSelect={() => go("/voices")}>
            <Mic2 className="mr-2 size-4" /> Voices
          </CommandItem>
          <CommandItem onSelect={() => go("/knowledge")}>
            <BookOpen className="mr-2 size-4" /> Knowledge Base
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
