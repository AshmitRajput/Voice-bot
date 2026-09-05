import { useState } from "react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import {
  useKnowledgeDocuments,
  useKnowledgeStats,
  useStoreKnowledgeDocument,
  useDeleteKnowledgeDocument,
} from "@/hooks/useKnowledgeBase";
import { KB_CATEGORIES } from "@/lib/types";

const STATUS_TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  pending: "outline",
  indexed: "default",
  stale: "secondary",
};

function humanize(key: string) {
  return key
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function KnowledgeBase() {
  const [category, setCategory] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [dialogOpen, setDialogOpen] = useState(false);

  const { data, isLoading, isError } = useKnowledgeDocuments(category, status);
  const { data: stats } = useKnowledgeStats();
  const storeDoc = useStoreKnowledgeDocument();
  const deleteDoc = useDeleteKnowledgeDocument();

  const [title, setTitle] = useState("");
  const [docCategory, setDocCategory] = useState<string>(KB_CATEGORIES[0]);
  const [content, setContent] = useState("");

  const resetForm = () => {
    setTitle("");
    setDocCategory(KB_CATEGORIES[0]);
    setContent("");
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    storeDoc.mutate(
      { title, category: docCategory, content },
      {
        onSuccess: () => {
          setDialogOpen(false);
          resetForm();
        },
      }
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <PageHeader
          title="Knowledge Base"
          description={
            stats ? `${stats.total_documents} documents · ${stats.embedding_model}` : undefined
          }
        />
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button>Add document</Button>
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>Add knowledge document</DialogTitle>
            </DialogHeader>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="kb-title">Title</Label>
                <Input
                  id="kb-title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Late payment policy — March 2026"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <Label>Category</Label>
                <Select value={docCategory} onValueChange={setDocCategory}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {KB_CATEGORIES.map((c) => (
                      <SelectItem key={c} value={c}>
                        {humanize(c)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="kb-content">Content</Label>
                <Textarea
                  id="kb-content"
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder="The exact policy text the agent should reference during calls…"
                  rows={8}
                  required
                />
              </div>

              {storeDoc.isError && (
                <p className="text-sm text-destructive">
                  Couldn't save this document — check the fields and try again.
                </p>
              )}

              <Button type="submit" disabled={storeDoc.isPending || !title || !content}>
                {storeDoc.isPending ? "Indexing…" : "Save and index"}
              </Button>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {stats && Object.keys(stats.by_category).length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Documents by category
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {Object.entries(stats.by_category).map(([cat, count]) => (
              <Badge key={cat} variant="outline">
                {cat === "(uncategorized)" ? cat : humanize(cat)}: {count}
              </Badge>
            ))}
          </CardContent>
        </Card>
      )}

      <div className="flex gap-3">
        <Select value={category ?? "all"} onValueChange={(v) => setCategory(v === "all" ? undefined : v)}>
          <SelectTrigger className="w-56">
            <SelectValue placeholder="Filter by category" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All categories</SelectItem>
            {KB_CATEGORIES.map((c) => (
              <SelectItem key={c} value={c}>
                {humanize(c)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select value={status ?? "all"} onValueChange={(v) => setStatus(v === "all" ? undefined : v)}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Filter by status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="indexed">Indexed</SelectItem>
            <SelectItem value="stale">Stale</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load documents. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/kb/documents/</code>.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Title</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Chunks</TableHead>
                <TableHead>Indexed at</TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.documents.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                    No documents yet — add one to get started.
                  </TableCell>
                </TableRow>
              )}
              {data?.documents.map((doc) => (
                <TableRow key={doc.id}>
                  <TableCell className="font-medium">{doc.title}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {doc.category ? humanize(doc.category) : "—"}
                  </TableCell>
                  <TableCell>
                    <Badge variant={STATUS_TONE[doc.status] ?? "outline"}>{doc.status}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{doc.chunk_count}</TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {doc.indexed_at ?? "Not indexed"}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      disabled={deleteDoc.isPending}
                      onClick={() => {
                        if (confirm(`Delete "${doc.title}"? This can't be undone.`)) {
                          deleteDoc.mutate(doc.doc_id);
                        }
                      }}
                    >
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}