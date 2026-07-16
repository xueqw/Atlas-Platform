import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";

export default function Home() {
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="grid gap-8 max-w-2xl w-full">
        <div className="text-center space-y-4">
          <h1 className="text-4xl font-bold tracking-tight">智能体工厂</h1>
          <p className="text-muted-foreground text-lg">
            通过可视化工作台构建、配置和部署 AI 智能体
          </p>
        </div>
        <div className="grid grid-cols-2 gap-6">
          <Card>
            <CardHeader>
              <CardTitle>工程师工作台</CardTitle>
              <CardDescription>
                通过构建向导或手动创建智能体，配置提示词、模型、知识库和工具
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/workbench">
                <Button className="w-full">进入工作台</Button>
              </Link>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>能力库</CardTitle>
              <CardDescription>
                管理提示词模板、模型配置和工具定义，供构建向导检索
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/capabilities">
                <Button className="w-full" variant="secondary">浏览能力库</Button>
              </Link>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>用户对话</CardTitle>
              <CardDescription>
                在简洁的界面中与已发布的智能体进行对话
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Link href="/chat">
                <Button className="w-full" variant="secondary">进入对话</Button>
              </Link>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}