import logging
from typing import Any, Dict, List, Optional

import requests
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.llms import LLM
from langchain_core.pydantic_v1 import Field
from langchain_core.utils import from_env, get_from_dict_or_env, pre_init

logger = logging.getLogger(__name__)

DEFAULT_TIME_OUT = 300
DEFAULT_CONTENT_TYPE_JSON = "application/json"


class OCIModelDeploymentLLM(LLM):
    """Base class for LLM deployed on OCI Data Science Model Deployment."""

    auth: dict = Field(default_factory=dict, exclude=True)
    """ADS auth dictionary for OCI authentication:
    https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html.
    This can be generated by calling `ads.common.auth.api_keys()`
    or `ads.common.auth.resource_principal()`. If this is not
    provided then the `ads.common.default_signer()` will be used."""

    max_tokens: int = 256
    """Denotes the number of tokens to predict per generation."""

    temperature: float = 0.2
    """A non-negative float that tunes the degree of randomness in generation."""

    k: int = 0
    """Number of most likely tokens to consider at each step."""

    p: float = 0.75
    """Total probability mass of tokens to consider at each step."""

    endpoint: str = Field(default_factory=from_env("OCI_LLM_ENDPOINT", default=""))
    """The uri of the endpoint from the deployed Model Deployment model."""

    best_of: int = 1
    """Generates best_of completions server-side and returns the "best"
    (the one with the highest log probability per token).
    """

    stop: Optional[List[str]] = None
    """Stop words to use when generating. Model output is cut off
    at the first occurrence of any of these substrings."""

    @pre_init
    def validate_environment(  # pylint: disable=no-self-argument
        cls, values: Dict
    ) -> Dict:
        """Validate that python package exists in environment."""
        try:
            import ads

        except ImportError as ex:
            raise ImportError(
                "Could not import ads python package. "
                "Please install it with `pip install oracle_ads`."
            ) from ex
        if not values.get("auth", None):
            values["auth"] = ads.common.auth.default_signer()
        return values

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Default parameters for the model."""
        raise NotImplementedError

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {
            **{"endpoint": self.endpoint},
            **self._default_params,
        }

    def _construct_json_body(self, prompt: str, params: dict) -> dict:
        """Constructs the request body as a dictionary (JSON)."""
        raise NotImplementedError

    def _invocation_params(self, stop: Optional[List[str]], **kwargs: Any) -> dict:
        """Combines the invocation parameters with default parameters."""
        params = self._default_params
        if self.stop is not None and stop is not None:
            raise ValueError("`stop` found in both the input and default params.")
        elif self.stop is not None:
            params["stop"] = self.stop
        elif stop is not None:
            params["stop"] = stop
        else:
            # Don't set "stop" in param as None. It should be a list.
            params["stop"] = []

        return {**params, **kwargs}

    def _process_response(self, response_json: dict) -> str:
        raise NotImplementedError

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call out to OCI Data Science Model Deployment endpoint.

        Args:
            prompt (str):
                The prompt to pass into the model.
            stop (List[str], Optional):
                List of stop words to use when generating.
            kwargs:
                requests_kwargs:
                    Additional ``**kwargs`` to pass to requests.post

        Returns:
            The string generated by the model.

        Example:
            .. code-block:: python

                response = oci_md("Tell me a joke.")

        """
        requests_kwargs = kwargs.pop("requests_kwargs", {})
        params = self._invocation_params(stop, **kwargs)
        body = self._construct_json_body(prompt, params)
        logger.info(f"LLM API Request:\n{prompt}")
        response = self._send_request(
            data=body, endpoint=self.endpoint, **requests_kwargs
        )
        completion = self._process_response(response)
        logger.info(f"LLM API Completion:\n{completion}")
        return completion

    def _send_request(
        self,
        data: Any,
        endpoint: str,
        header: Optional[dict] = {},
        **kwargs: Any,
    ) -> Dict:
        """Sends request to the oci data science model deployment endpoint.

        Args:
            data (Json serializable):
                data need to be sent to the endpoint.
            endpoint (str):
                The model HTTP endpoint.
            header (dict, optional):
                A dictionary of HTTP headers to send to the specified url.
                Defaults to {}.
            kwargs:
                Additional ``**kwargs`` to pass to requests.post.
        Raises:
            Exception:
                Raise when invoking fails.

        Returns:
            A JSON representation of a requests.Response object.
        """
        if not header:
            header = {}
        header["Content-Type"] = (
            header.pop("content_type", DEFAULT_CONTENT_TYPE_JSON)
            or DEFAULT_CONTENT_TYPE_JSON
        )
        request_kwargs = {"json": data}
        request_kwargs["headers"] = header
        timeout = kwargs.pop("timeout", DEFAULT_TIME_OUT)

        attempts = 0
        while attempts < 2:
            request_kwargs["auth"] = self.auth.get("signer")
            response = requests.post(
                endpoint, timeout=timeout, **request_kwargs, **kwargs
            )
            if response.status_code == 401:
                self._refresh_signer()
                attempts += 1
                continue
            break

        try:
            response.raise_for_status()
            response_json = response.json()

        except Exception:
            logger.error(
                "DEBUG INFO: request_kwargs=%s, status_code=%s, content=%s",
                request_kwargs,
                response.status_code,
                response.content,
            )
            raise

        return response_json

    def _refresh_signer(self) -> None:
        if self.auth.get("signer", None) and hasattr(
            self.auth["signer"], "refresh_security_token"
        ):
            self.auth["signer"].refresh_security_token()


class OCIModelDeploymentTGI(OCIModelDeploymentLLM):
    """OCI Data Science Model Deployment TGI Endpoint.

    To use, you must provide the model HTTP endpoint from your deployed
    model, e.g. https://<MD_OCID>/predict.

    To authenticate, `oracle-ads` has been used to automatically load
    credentials: https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html

    Make sure to have the required policies to access the OCI Data
    Science Model Deployment endpoint. See:
    https://docs.oracle.com/en-us/iaas/data-science/using/model-dep-policies-auth.htm#model_dep_policies_auth__predict-endpoint

    Example:
        .. code-block:: python

            from langchain_community.llms import ModelDeploymentTGI

            oci_md = ModelDeploymentTGI(endpoint="https://<MD_OCID>/predict")

    """

    do_sample: bool = True
    """If set to True, this parameter enables decoding strategies such as
    multi-nominal sampling, beam-search multi-nominal sampling, Top-K
    sampling and Top-p sampling.
    """

    watermark: bool = True
    """Watermarking with `A Watermark for Large Language Models <https://arxiv.org/abs/2301.10226>`_.
    Defaults to True."""

    return_full_text: bool = False
    """Whether to prepend the prompt to the generated text. Defaults to False."""

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_model_deployment_tgi_endpoint"

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for invoking OCI model deployment TGI endpoint."""
        return {
            "best_of": self.best_of,
            "max_new_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_k": self.k
            if self.k > 0
            else None,  # `top_k` must be strictly positive'
            "top_p": self.p,
            "do_sample": self.do_sample,
            "return_full_text": self.return_full_text,
            "watermark": self.watermark,
        }

    def _construct_json_body(self, prompt: str, params: dict) -> dict:
        return {
            "inputs": prompt,
            "parameters": params,
        }

    def _process_response(self, response_json: dict) -> str:
        return str(response_json.get("generated_text", response_json)) + "\n"


class OCIModelDeploymentVLLM(OCIModelDeploymentLLM):
    """VLLM deployed on OCI Data Science Model Deployment

    To use, you must provide the model HTTP endpoint from your deployed
    model, e.g. https://<MD_OCID>/predict.

    To authenticate, `oracle-ads` has been used to automatically load
    credentials: https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html

    Make sure to have the required policies to access the OCI Data
    Science Model Deployment endpoint. See:
    https://docs.oracle.com/en-us/iaas/data-science/using/model-dep-policies-auth.htm#model_dep_policies_auth__predict-endpoint

    Example:
        .. code-block:: python

            from langchain_community.llms import OCIModelDeploymentVLLM

            oci_md = OCIModelDeploymentVLLM(
                endpoint="https://<MD_OCID>/predict",
                model="mymodel"
            )

    """

    model: str
    """The name of the model."""

    n: int = 1
    """Number of output sequences to return for the given prompt."""

    k: int = -1
    """Number of most likely tokens to consider at each step."""

    frequency_penalty: float = 0.0
    """Penalizes repeated tokens according to frequency. Between 0 and 1."""

    presence_penalty: float = 0.0
    """Penalizes repeated tokens. Between 0 and 1."""

    use_beam_search: bool = False
    """Whether to use beam search instead of sampling."""

    ignore_eos: bool = False
    """Whether to ignore the EOS token and continue generating tokens after
    the EOS token is generated."""

    logprobs: Optional[int] = None
    """Number of log probabilities to return per output token."""

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_model_deployment_vllm_endpoint"

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling vllm."""
        return {
            "best_of": self.best_of,
            "frequency_penalty": self.frequency_penalty,
            "ignore_eos": self.ignore_eos,
            "logprobs": self.logprobs,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "n": self.n,
            "presence_penalty": self.presence_penalty,
            "stop": self.stop,
            "temperature": self.temperature,
            "top_k": self.k,
            "top_p": self.p,
            "use_beam_search": self.use_beam_search,
        }

    def _construct_json_body(self, prompt: str, params: dict) -> dict:
        return {
            "prompt": prompt,
            **params,
        }

    def _process_response(self, response_json: dict) -> str:
        return response_json["choices"][0]["text"]
