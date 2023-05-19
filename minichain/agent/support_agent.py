from __future__ import annotations

import json
from string import Template
from typing import Any, List, Optional, Sequence, Dict, Tuple, Union

from pydantic import BaseModel, Extra

from minichain.agent.output_parser import ConvoJSONOutputParser
from minichain.agent.prompt import CA2_PREFIX_PROMPT, SBS_SUFFIX, SBS_FORMAT_INSTRUCTIONS, \
    FIX_TOOL_INPUT_PROMPT_FORMAT
from minichain.agent.prompt_formatter import JSONPromptTemplate
from minichain.memory.message import HumanMessage, BaseMessage
from minichain.models.base import Generation, BaseLanguageModel
from minichain.structs import AgentAction, AgentFinish
from minichain.tools.base import Tool
from minichain.tools.tools import HandOffToAgent


class SupportAgent(BaseModel):
    output_parser: ConvoJSONOutputParser = ConvoJSONOutputParser()
    action_history: Dict[str, Any] = {}
    llm: BaseLanguageModel = None
    allowed_tools: Optional[List[str]] = None
    prompt_template: JSONPromptTemplate = None

    class Config:
        """Configuration for this pydantic object."""
        extra = Extra.forbid
        arbitrary_types_allowed = True

    @classmethod
    def _get_default_output_parser(
        cls, ai_prefix: str = "AI", **kwargs: Any
    ) -> ConvoJSONOutputParser:
        return ConvoJSONOutputParser()

    # @classmethod
    # def create_function_description(cls, llm, func):
    #     code = inspect.getsource(func)
    #     prompt = CODE_DESCRIPTION_PROMPT_FORMAT.format(code=code)
    #     print(f"prompt: {prompt}")
    #     output = llm.generate([prompt])
    #     print(f"output: {output}")
    #
    #     function_desp = output.generations[0][0].text
    #     args = inspect.getfullargspec(func).args
    #     s = ""
    #     for arg in args:
    #         s += f"<'{arg}'>, "
    #
    #     args_desp = f"""Action Input: {s[:-2]}"""
    #     return f"{function_desp}\n{args_desp}"

    # @classmethod
    # def update_tool_desp(cls, llm, tools):
    #     for tool in tools:
    #         if hasattr(tool, "func"):
    #             tool.expected_outcome = cls.create_function_description(llm, tool.func)

    @classmethod
    def from_llm_and_tools(
        cls,
        llm: BaseLanguageModel,
        tools: Sequence[Tool],
        output_parser: Optional[ConvoJSONOutputParser] = None,
        prefix: str = CA2_PREFIX_PROMPT,
        suffix: str = SBS_SUFFIX,
        format_instructions: str = SBS_FORMAT_INSTRUCTIONS,
        ai_prefix: str = "AI",
        human_prefix: str = "Human",
        policy_desp: str = "",
        input_variables: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> SupportAgent:
        """Construct an agent from an LLM and tools."""
        # TODO: disable to save cost now
        # cls._validate_tools(tools)
        # cls.update_tool_desp(llm, tools)

        tools.append(HandOffToAgent())

        prompt_template = cls.get_prompt_template(
            tools,
            ai_prefix=ai_prefix,
            human_prefix=human_prefix,
            prefix=prefix.format(policy=policy_desp),
            suffix=suffix,
            format_instructions=format_instructions,
            input_variables=input_variables,
        )
        # print(f">>>Prompt: {prompt} \n")
        tool_names = [tool.name for tool in tools]
        _output_parser = output_parser or ConvoJSONOutputParser()
        return cls(
            llm=llm,
            allowed_tools=tool_names,
            output_parser=_output_parser,
            prompt_template=prompt_template,
            **kwargs,
        )

    def _construct_scratchpad(
        self, intermediate_steps: List[AgentAction]
    ) -> Union[str, List[BaseMessage]]:
        """Construct the scratchpad that lets the agent continue its thought process."""
        thoughts = ""
        for action in intermediate_steps:
            # thoughts += action.log
            thoughts += f"\nObservation from using tool '{action.tool}' is '{action.observation}'\n"
        return thoughts

    def get_final_prompt(
        self, intermediate_steps: List[Tuple[AgentAction, str]], **kwargs: Any
    ) -> List[BaseMessage]:
        """Create the full inputs for the LLMChain from intermediate steps."""
        thoughts = self._construct_scratchpad(intermediate_steps)
        new_inputs = {"agent_scratchpad": thoughts}
        full_inputs = {**kwargs, **new_inputs}
        prompt = self.prompt_template.format_prompt(**full_inputs)
        return prompt

    @classmethod
    def get_prompt_template(
        cls,
        tools: Sequence[Tool],
        prefix: str = CA2_PREFIX_PROMPT,
        suffix: str = SBS_SUFFIX,
        format_instructions: str = SBS_FORMAT_INSTRUCTIONS,
        ai_prefix: str = "AI",
        human_prefix: str = "Human",
        input_variables: Optional[List[str]] = None,
    ) -> JSONPromptTemplate:

        """Create prompt in the style of the zero shot agent.

        Args:
            tools: List of tools the agent will have access to, used to format the
                prompt.
            prefix: String to put before the list of tools.
            suffix: String to put after the list of tools.
            format_instructions: part of the prompt that format response from model
            ai_prefix: String to use before AI output.
            human_prefix: String to use before human output.
            input_variables: List of input variables the final prompt will expect.

        Returns:
            A PromptTemplate with the template assembled from the pieces here.
        """
        tool_strings = "\n".join(
            [f"> {tool.name}: {tool.description}" for tool in tools]
        )
        tool_names = ", ".join([tool.name for tool in tools])
        t = Template(format_instructions)
        format_instructions = t.substitute(
            tool_names=tool_names, ai_prefix=ai_prefix, human_prefix=human_prefix
        )
        template = Template("\n\n".join([prefix, tool_strings, suffix, format_instructions, ]))
        if input_variables is None:
            input_variables = ["input", "chat_history", "agent_scratchpad"]
        return JSONPromptTemplate(template=template, input_variables=input_variables)

    def plan(
        self, intermediate_steps: List[Tuple[AgentAction, str]], **kwargs: Any
    ) -> Union[AgentAction, AgentFinish]:
        print(f"Inputs: {kwargs}, intermediate_steps: {intermediate_steps}")
        final_prompt = self.get_final_prompt(intermediate_steps, **kwargs)
        print(f"Full Input: {final_prompt[0].content} \n")
        full_output: Generation = self.llm.generate(final_prompt).generations[0]
        agent_output: Union[AgentAction, AgentFinish] = self.output_parser.parse(
            full_output.message.content)

        print(f"Full output: {json.loads(full_output.message.content)}")
        if isinstance(agent_output, AgentAction):
            print(f"Take action {agent_output.tool}")
            # call hand off to agent and finish workflow
            if agent_output.tool == HandOffToAgent().name:
                return AgentFinish(
                    return_values={"output": HandOffToAgent().run("")},
                    log=f"Handing off to agent"
                )

            # Prevent agent take the same action again
            if (agent_output.tool in self.action_history
                and str(agent_output.tool_input) in self.action_history[str(agent_output.tool)]
            ):
                if agent_output.response:
                    return AgentFinish(
                        return_values={"output": agent_output.response},
                        log=f"Action taken before: {agent_output.tool}, "
                            f"input: {agent_output.tool_input}"
                    )
                else:
                    return AgentFinish(
                        return_values={"output": HandOffToAgent().run("")},
                        log=f"Handing off to agent"
                    )

            self.action_history[agent_output.tool] = str(agent_output.tool_input)
        return agent_output

    def fix_action_input(self, tool: Tool, action: AgentAction, error: str) -> AgentAction:
        prompt = FIX_TOOL_INPUT_PROMPT_FORMAT.format(tool_description=tool.description,
                                                     inputs=action.tool_input,
                                                     error=error)

        print(f"Fixing tool input prompt: {prompt}")
        messages = HumanMessage(content=prompt)
        output = self.llm.generate([messages])
        text = output.generations[0].message.content
        inputs = text[text.index("{"):text.rindex("}") + 1].strip()
        new_tool_inputs = json.loads(inputs)

        print(f"Fixed tool input: {new_tool_inputs}")
        new_action = AgentAction(tool=action.tool, tool_input=new_tool_inputs, log=action.log)
        return new_action