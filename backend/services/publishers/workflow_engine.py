"""
工作流引擎 - 配置驱动的浏览器自动化
"""

from dataclasses import dataclass, field
from typing import Any, Optional
from playwright.async_api import Page
import yaml
import tempfile
import os
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """动作执行结果"""
    success: bool
    message: str = ""
    data: Any = None


@dataclass
class PublishContext:
    """发布上下文，存储变量供工作流使用"""
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    category: str = ""
    article_type: str = "original"
    pub_type: str = "public"
    
    def get_variable(self, name: str) -> Any:
        """获取变量值"""
        return getattr(self, name, "")


class ActionExecutor:
    """动作执行器"""
    
    def __init__(self, page: Page, context: PublishContext):
        self.page = page
        self.context = context
        self._temp_file_path: Optional[str] = None
    
    def _replace_variables(self, value: str) -> str:
        """替换模板变量 {{variable}}"""
        if not isinstance(value, str):
            return value
        pattern = r'\{\{(\w+)\}\}'
        def replacer(match):
            var_name = match.group(1)
            return str(self.context.get_variable(var_name))
        return re.sub(pattern, replacer, value)
    
    async def execute(self, action: dict) -> ActionResult:
        """执行单个动作"""
        action_type = action.get('action')
        name = action.get('name', action_type)
        
        try:
            handler = getattr(self, f'_action_{action_type}', None)
            if not handler:
                return ActionResult(False, f"未知动作类型: {action_type}")
            
            result = await handler(action)
            
            wait_after = action.get('wait_after', 0)
            if wait_after:
                await self.page.wait_for_timeout(wait_after)
            
            wait_for = action.get('wait_for')
            if wait_for:
                await self.page.wait_for_selector(wait_for, state='visible', timeout=10000)
            
            logger.info(f"[{name}] 执行成功")
            return result
            
        except Exception as e:
            if action.get('optional', False):
                logger.warning(f"[{name}] 可选动作失败: {e}")
                return ActionResult(True, f"可选动作跳过: {e}")
            logger.error(f"[{name}] 执行失败: {e}")
            return ActionResult(False, str(e))
    
    async def _action_click(self, action: dict) -> ActionResult:
        """点击元素"""
        selector = self._replace_variables(action['selector'])
        await self.page.click(selector)
        return ActionResult(True)
    
    async def _action_js_click(self, action: dict) -> ActionResult:
        """通过 JS 点击"""
        script = action['script']
        await self.page.evaluate(script)
        return ActionResult(True)
    
    async def _action_js_eval(self, action: dict) -> ActionResult:
        """执行 JS 脚本"""
        script = action['script']
        await self.page.evaluate(script)
        return ActionResult(True)
    
    async def _action_fill(self, action: dict) -> ActionResult:
        """填充输入框"""
        selector = self._replace_variables(action['selector'])
        value = self._replace_variables(action['value'])
        await self.page.fill(selector, value)
        return ActionResult(True)
    
    async def _action_select(self, action: dict) -> ActionResult:
        """选择下拉框"""
        selector = self._replace_variables(action['selector'])
        value = self._replace_variables(action['value'])
        await self.page.evaluate(f'''(value) => {{
            const el = document.querySelector('{selector}');
            if (el) {{
                el.value = value;
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        }}''', value)
        return ActionResult(True)
    
    async def _action_click_by_text(self, action: dict) -> ActionResult:
        """根据文本内容点击"""
        container = action['container']
        item_selector = action['item_selector']
        text = self._replace_variables(action['text'])
        
        await self.page.evaluate(f'''(text) => {{
            document.querySelectorAll('{container} {item_selector}').forEach(el => {{
                if (el.textContent.trim() === text) el.click();
            }});
        }}''', text)
        return ActionResult(True)
    
    async def _action_tag_input(self, action: dict) -> ActionResult:
        """标签输入（特殊处理）"""
        tags = self.context.tags
        if not tags:
            return ActionResult(True, "无标签")
        
        click_selector = action['click_selector']
        type_delay = action.get('type_delay', 100)
        select_selector = action['select_selector']
        
        await self.page.click(click_selector)
        await self.page.keyboard.type(tags[0], delay=type_delay)
        await self.page.wait_for_timeout(1000)
        
        hover_option = await self.page.query_selector(select_selector)
        if hover_option:
            await hover_option.click()
        
        return ActionResult(True)
    
    async def _action_tag_input_csdn(self, action: dict) -> ActionResult:
        """CSDN 标签输入 - 使用自动提取的标签"""
        tags = self.context.tags
        if not tags:
            logger.info("[CSDN标签] 无标签，跳过")
            return ActionResult(True, "无标签")
        
        logger.info(f"[CSDN标签] 准备填写标签: {tags}")
        
        for tag in tags[:3]:  # CSDN 最多3个标签
            try:
                # 点击添加标签按钮
                add_btn = await self.page.query_selector('.tag__btn-tag, .mark_selection_box .tag__btn-tag')
                if add_btn:
                    await add_btn.click()
                    await self.page.wait_for_timeout(500)
                
                # 在输入框中输入标签
                tag_input = await self.page.query_selector('.mark_selection_box input, .tag-input input, input[placeholder*="标签"], .mark_add_tag_left input')
                if tag_input:
                    await tag_input.fill('')  # 先清空
                    await tag_input.fill(tag)
                    await self.page.wait_for_timeout(800)
                    
                    # 尝试点击下拉建议中的第一个匹配项
                    suggestion = await self.page.query_selector('.mark_add_tag_left .el-tag, .tag-suggestion .el-tag, .mark_add_tag_left li')
                    if suggestion:
                        await suggestion.click()
                        logger.info(f"[CSDN标签] 已选择建议标签: {tag}")
                    else:
                        # 如果没有建议，直接按回车
                        await self.page.keyboard.press('Enter')
                        logger.info(f"[CSDN标签] 已输入标签: {tag}")
                    
                    await self.page.wait_for_timeout(500)
            except Exception as e:
                logger.warning(f"[CSDN标签] 添加标签 '{tag}' 失败: {e}")
        
        # 关闭标签弹窗（如果有的话）
        try:
            mask = await self.page.query_selector('.mark-mask-box-div')
            if mask:
                await mask.click()
                await self.page.wait_for_timeout(300)
        except:
            pass
        
        return ActionResult(True)
    
    async def _action_file_upload(self, action: dict) -> ActionResult:
        """文件上传"""
        selector = action['selector']
        file_input = await self.page.query_selector(selector)
        if file_input and self._temp_file_path:
            await file_input.set_input_files(self._temp_file_path)
        return ActionResult(True)
    
    async def _action_type(self, action: dict) -> ActionResult:
        """键盘输入"""
        text = self._replace_variables(action.get('text', ''))
        delay = action.get('delay', 50)
        await self.page.keyboard.type(text, delay=delay)
        return ActionResult(True)


class WorkflowEngine:
    """工作流引擎"""
    
    def __init__(self, config_dir: str = None):
        self.config_dir = config_dir or os.path.join(
            os.path.dirname(__file__), 'configs'
        )
        self.configs: dict[str, dict] = {}
    
    def load_config(self, platform_id: str) -> dict:
        """加载平台配置"""
        if platform_id in self.configs:
            return self.configs[platform_id]
        
        config_path = os.path.join(self.config_dir, f'{platform_id}.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        self.configs[platform_id] = config
        return config
    
    def get_supported_platforms(self) -> list[str]:
        """获取支持的平台列表"""
        platforms = []
        if os.path.exists(self.config_dir):
            for f in os.listdir(self.config_dir):
                if f.endswith('.yaml'):
                    platforms.append(f[:-5])
        return platforms
    
    async def execute_workflow(
        self,
        page: Page,
        config: dict,
        context: PublishContext
    ) -> ActionResult:
        """执行完整工作流"""
        executor = ActionExecutor(page, context)
        
        workflow = config.get('workflow', [])
        for step in workflow:
            result = await executor.execute(step)
            if not result.success:
                return result
        
        return ActionResult(True, "工作流执行完成")
    
    async def upload_content(
        self,
        page: Page,
        config: dict,
        context: PublishContext
    ) -> ActionResult:
        """上传内容"""
        upload_config = config.get('content_upload', {})
        upload_type = upload_config.get('type')
        
        if upload_type == 'file_upload':
            return await self._upload_via_file(page, upload_config, context)
        elif upload_type == 'codemirror':
            return await self._upload_via_codemirror(page, upload_config, context)
        elif upload_type == 'import_doc':
            return await self._upload_via_import(page, upload_config, context)
        elif upload_type == 'direct_input':
            return await self._upload_via_direct(page, upload_config, context)
        else:
            return ActionResult(False, f"未知上传类型: {upload_type}")
    
    async def _upload_via_file(self, page: Page, config: dict, context: PublishContext) -> ActionResult:
        """通过文件上传"""
        # 使用文章标题作为文件名（CSDN 会用文件名作为标题）
        safe_title = re.sub(r'[\\/*?:"<>|]', '', context.title)[:50]  # 移除非法字符，限制长度
        temp_dir = tempfile.gettempdir()
        md_path = os.path.join(temp_dir, f"{safe_title}.md")
        
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(context.content)
        
        try:
            selector = config['selector']
            file_input = await page.query_selector(selector)
            if file_input:
                await file_input.set_input_files(md_path)
                wait_after = config.get('wait_after', 3000)
                await page.wait_for_timeout(wait_after)
                return ActionResult(True)
            return ActionResult(False, "未找到文件上传元素")
        finally:
            if os.path.exists(md_path):
                os.unlink(md_path)
    
    async def _upload_via_codemirror(self, page: Page, config: dict, context: PublishContext) -> ActionResult:
        """通过 CodeMirror 设置内容"""
        title_selector = config.get('title_selector')
        if title_selector:
            await page.fill(title_selector, context.title)
        
        content_selector = config['content_selector']
        await page.evaluate(f'''(content) => {{
            const el = document.querySelector('{content_selector}');
            if (el && el.CodeMirror) {{
                el.CodeMirror.setValue(content);
            }}
        }}''', context.content)
        
        return ActionResult(True)
    
    async def _upload_via_import(self, page: Page, config: dict, context: PublishContext) -> ActionResult:
        """通过导入文档功能上传"""
        executor = ActionExecutor(page, context)
        
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.md', delete=False, encoding='utf-8'
        ) as f:
            f.write(context.content)
            executor._temp_file_path = f.name
        
        try:
            for step in config.get('steps', []):
                result = await executor.execute(step)
                if not result.success:
                    return result
            return ActionResult(True)
        finally:
            if executor._temp_file_path and os.path.exists(executor._temp_file_path):
                os.unlink(executor._temp_file_path)
    
    async def _upload_via_direct(self, page: Page, config: dict, context: PublishContext) -> ActionResult:
        """直接输入内容"""
        title_selector = config.get('title_selector')
        content_selector = config['content_selector']
        
        if title_selector:
            await page.fill(title_selector, context.title)
        
        await page.click(content_selector)
        await page.keyboard.type(context.content)
        
        return ActionResult(True)
    
    async def get_result_url(self, page: Page, config: dict) -> Optional[str]:
        """获取发布后的文章 URL"""
        result_config = config.get('result_url', {})
        result_type = result_config.get('type')
        
        if result_type == 'element_attribute':
            selector = result_config['selector']
            attribute = result_config['attribute']
            
            # 等待元素出现（最多10秒）
            for _ in range(10):
                url = await page.evaluate(f'''() => {{
                    const el = document.querySelector('{selector}');
                    return el ? el.getAttribute('{attribute}') : null;
                }}''')
                if url:
                    return url
                await page.wait_for_timeout(1000)
            return None
        
        elif result_type == 'js_eval':
            script = result_config['script']
            return await page.evaluate(script)
        
        elif result_type == 'current_url':
            return page.url
        
        return None
